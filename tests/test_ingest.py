"""T13 入库编排集成测试。

★ 依赖：PostgreSQL（pgvector）+ Ollama（bge-m3）+ silo 必须都在运行。
  这是集成测试，不是单元测试——"事务回滚不留半成品"必须用真实 DB 与对象存储验证。

验收：① 入库成功后 chunk 数与向量数一致；② 落库中途失败，DB 与对象存储都无残留。

★ 隔离：全部数据写在专用 kb_id=999999 下，避开开发库里的真实数据（kb_id=1），
  且每个测试前后都会清理该 kb 的残留（fixture），可重复跑、不污染。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from minio import Minio
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.embedding import embed_texts
from app.core.storage import (
    StorageConfig,
    delete_prefix,
    get_client,
    get_text,
    load_config,
)
from app.repos.chunk_repo import search_chunks
from app.repos.document_repo import find_active_by_md5
from app.repos.models import Chunk, Document
from app.services.file_validator import md5_of_bytes
from app.services.ingest import ingest_document

# 专用验收知识库 id：不与开发库真实数据（kb_id=1）混在一起
KB_ID = 999999

SAMPLE_TITLE = "t13_sample.md"
SAMPLE_TEXT = """# T13 入库验收样例

第三条 报销上限。单次出差住宿报销上限 8812 元，超出部分自理。

第四条 提交时限。报销单须在出差结束后 15 个工作日内提交。
"""

# 回滚用例用的另一份内容（md5 与 SAMPLE_TEXT 不同，避免去重干扰）
ROLLBACK_TEXT = "T13 回滚用例：这份内容只用于验证失败不留半成品。"


def _services_available() -> bool:
    """PG / Ollama / silo 是否全部可达（缺任意一个则整组跳过，而非判失败）。"""
    try:
        config = load_config()
        get_client(config).bucket_exists(config.bucket)
    except Exception:
        return False
    try:
        embed_texts(["ping"])
    except Exception:
        return False
    try:
        search_chunks([0.0] * 1024, ["internal"], top_k=1)
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="PostgreSQL / Ollama(bge-m3) / silo 未启动，跳过入库编排集成测试",
)


def _purge(engine: Engine, client: Minio, config: StorageConfig) -> None:
    """清掉 kb_id=KB_ID 下的全部文档、其 chunks，以及它们对应的对象前缀。"""
    with Session(engine) as session:
        rows = session.execute(
            select(Document.id, Document.storage_key).where(Document.kb_id == KB_ID)
        ).all()
        if rows:
            doc_ids = [r[0] for r in rows]
            session.execute(delete(Chunk).where(Chunk.doc_id.in_(doc_ids)))
            session.execute(delete(Document).where(Document.id.in_(doc_ids)))
            session.commit()

    for _, storage_key in rows:
        if storage_key:
            prefix = storage_key.rsplit("/", 1)[0] + "/"
            delete_prefix(prefix, client=client, config=config)


@pytest.fixture()
def services() -> Iterator[tuple[Engine, Minio, StorageConfig]]:
    """提供 (engine, client, config)，并保证测试前后都不留 kb_id=999999 的残留。"""
    config = load_config()
    client = get_client(config)
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)

    _purge(engine, client, config)
    yield engine, client, config
    _purge(engine, client, config)


class TestIngest:
    """T13 的两条验收标准（docs/tasks.md V0.1 表 T13 行）。"""

    def test_chunk_count_matches_vector_count(
        self, services: tuple[Engine, Minio, StorageConfig]
    ) -> None:
        """① 入库成功后：chunk 数与向量数一致，且库行与对象都真实存在。"""
        engine, client, config = services
        data = SAMPLE_TEXT.encode("utf-8")

        with Session(engine) as session:
            result = ingest_document(
                session, kb_id=KB_ID, filename=SAMPLE_TITLE, data=data
            )

        assert result.chunk_count == result.vector_count, "chunk 数与向量数必须一致"

        with Session(engine) as session:
            db_rows = session.scalar(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.doc_id == result.doc_id)
            )
        assert int(db_rows or 0) == result.vector_count, "库中 chunk 行数必须等于向量数"

        # 原文与解析文本各落成一个对象
        assert get_text(result.raw_key, client=client, config=config) == SAMPLE_TEXT
        assert get_text(result.parsed_key, client=client, config=config).strip() != ""

        # scope_key 与 embedding 写在同一行（R2 权限过滤下沉 SQL 的前提）
        with Session(engine) as session:
            scopes = set(
                session.scalars(
                    select(Chunk.scope_key).where(Chunk.doc_id == result.doc_id)
                )
            )
        assert scopes == {"internal"}

    def test_rollback_leaves_no_partial_state(
        self,
        services: tuple[Engine, Minio, StorageConfig],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """② 落库中途失败：DB 无残留、对象存储无孤儿对象（补偿删除生效）。"""
        engine, client, config = services
        data = ROLLBACK_TEXT.encode("utf-8")

        def _object_names() -> set[str]:
            return {
                obj.object_name
                for obj in client.list_objects(
                    config.bucket, prefix="docs/", recursive=True
                )
            }

        before = _object_names()

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("注入的写入失败（模拟落库中途出错）")

        # 让 documents 已 flush、chunks 写入抛错，走 ingest 的回滚 + 补偿分支
        monkeypatch.setattr("app.services.ingest.insert_chunks", _boom)

        with Session(engine) as session:
            with pytest.raises(RuntimeError, match="注入的写入失败"):
                ingest_document(
                    session, kb_id=KB_ID, filename="t13_rollback.md", data=data
                )

        # ① DB 无残留
        with Session(engine) as session:
            assert (
                find_active_by_md5(session, kb_id=KB_ID, file_md5=md5_of_bytes(data))
                is None
            )
            remaining = session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.kb_id == KB_ID)
            )
        assert int(remaining or 0) == 0, "回滚后 documents 不应有残留行"

        # ② 对象存储无残留（刚写的 docs/{uuid}/ 已被补偿删除）
        assert _object_names() == before, "回滚后不应留下孤儿对象"
