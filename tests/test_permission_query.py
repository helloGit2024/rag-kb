"""T15 越权验收：权限过滤在检索 SQL 内生效（R2）。

验收标准（tasks.md T15）：用销售部账号检索，结果中**不出现任何**研发部 doc。

做法：造两个用户（dept=销售部 / dept=研发部）与两份文档（scope 各自对应），
用一份与研发部文档高度相似的查询向量去检索：
  · 研发部账号 → 命中研发部文档（证明该文档**确实可被召回**，不是分数太低）；
  · 销售部账号 → **不命中**该文档（证明是权限过滤在 SQL 内把它排除，而非碰巧）。
数据自带创建与清理，不依赖预先灌入的演示账号。

依赖 PostgreSQL（pgvector）与 Ollama(bge-m3)，不可达时跳过（同 test_retrieval）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.embedding import embed_texts
from app.repos.chunk_repo import search_chunks
from app.repos.db import get_engine
from app.repos.models import Chunk, Document, User, UserScopeGrant
from app.services.permission import document_scope_key, resolve_scope_keys

SALES_USER = "t15_sales"
RND_USER = "t15_rnd"
SALES_TITLE = "T15 测试-销售部文档"
RND_TITLE = "T15 测试-研发部文档"
RND_CONTENT = "研发部机密：新产品代号 XH-2026，仅供研发部内部查看。"
SALES_CONTENT = "销售部通知：本月销售目标为 1204 万元。"


def _services_available() -> bool:
    """检查 PG 与嵌入服务是否可达（不可达则跳过测试）。"""
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
    reason="PostgreSQL 或 Ollama(bge-m3) 未启动，跳过越权集成测试",
)


def _cleanup(session: Session) -> None:
    """删除本测试可能遗留的数据（先删引用方，再删被引用方）。"""
    doc_ids = session.scalars(
        select(Document.id).where(Document.title.in_([RND_TITLE, SALES_TITLE]))
    ).all()
    if doc_ids:
        session.execute(delete(Chunk).where(Chunk.doc_id.in_(doc_ids)))
        session.execute(delete(Document).where(Document.id.in_(doc_ids)))
    user_ids = session.scalars(
        select(User.id).where(User.username.in_([SALES_USER, RND_USER]))
    ).all()
    if user_ids:
        session.execute(
            delete(UserScopeGrant).where(UserScopeGrant.user_id.in_(user_ids))
        )
        session.execute(delete(User).where(User.id.in_(user_ids)))
    session.commit()


def _setup(session: Session) -> int:
    """造用户 + 文档 + 块，返回研发部文档的 id。"""
    _cleanup(session)  # 幂等：清掉上次残留

    session.add_all(
        [
            User(
                username=SALES_USER,
                display_name="销售测试",
                dept="销售部",
                role="user",
                status="active",
            ),
            User(
                username=RND_USER,
                display_name="研发测试",
                dept="研发部",
                role="user",
                status="active",
            ),
        ]
    )
    session.flush()

    rnd_doc = Document(
        title=RND_TITLE, source_type="txt", scope="dept:研发部", status="done"
    )
    sales_doc = Document(
        title=SALES_TITLE, source_type="txt", scope="dept:销售部", status="done"
    )
    session.add_all([rnd_doc, sales_doc])
    session.flush()

    session.add_all(
        [
            Chunk(
                doc_id=rnd_doc.id,
                kb_id=1,
                chunk_index=0,
                content=RND_CONTENT,
                char_start=0,
                char_end=len(RND_CONTENT),
                embedding=embed_texts([RND_CONTENT])[0],
                scope_key=document_scope_key("dept:研发部"),
            ),
            Chunk(
                doc_id=sales_doc.id,
                kb_id=1,
                chunk_index=0,
                content=SALES_CONTENT,
                char_start=0,
                char_end=len(SALES_CONTENT),
                embedding=embed_texts([SALES_CONTENT])[0],
                scope_key=document_scope_key("dept:销售部"),
            ),
        ]
    )
    session.commit()
    return rnd_doc.id


def test_sales_account_cannot_retrieve_rnd_doc():
    """销售部账号检索：结果中不出现研发部 doc；研发部账号则能命中（对照）。"""
    engine = get_engine()
    with Session(engine) as session:
        rnd_doc_id = _setup(session)
        try:
            rnd_keys = resolve_scope_keys(session, username=RND_USER)
            sales_keys = resolve_scope_keys(session, username=SALES_USER)

            # 前置断言：范围解析正确（研发部账号含 dept:研发部，销售部账号不含）
            assert "dept:研发部" in rnd_keys
            assert "dept:研发部" not in sales_keys

            # 查询向量取研发部文档原文 → 若不做权限过滤，它必进 Top-1
            query_vec = embed_texts([RND_CONTENT])[0]

            rnd_hits = search_chunks(query_vec, rnd_keys, top_k=5)
            assert any(h.doc_id == rnd_doc_id for h in rnd_hits), (
                "研发部账号应能召回研发部文档；否则无法证明是权限过滤"
                "（而非该文档分数太低）"
            )

            sales_hits = search_chunks(query_vec, sales_keys, top_k=5)
            leaked = [h.doc_id for h in sales_hits if h.doc_id == rnd_doc_id]
            assert leaked == [], (
                f"销售部账号检索到研发部 doc：{leaked}（R2 被违反："
                f"权限过滤未在 SQL 内生效）"
            )
        finally:
            _cleanup(session)
