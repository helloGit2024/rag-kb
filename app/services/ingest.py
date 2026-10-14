"""入库服务编排（V0.1 · T13）。

职责：把一份已通过校验的文件，走完
    解析 → 分块 → 向量化 → 落对象存储 → 落库（单事务）
任一步失败都不留半成品（DB 事务回滚 + 对象存储补偿删除）。

事务与顺序设计（逐条对应一条硬规则 / 验收标准）：
  ① 校验 + 去重（只读，短事务）：尽早失败，不把算力浪费在重复文件上（T12）。
  ② 解析（纯计算，去页眉页脚，T10）。
  ③ 分块（纯计算，512/64，T03）。
  ④ 向量化（调嵌入服务；此处**不持有**任何 DB 事务/行锁）。
  ⑤ 写对象存储 raw + parsed —— key 由**预生成**的 doc_uuid 决定，与 DB 自增 id 无关。
  ⑥ 单事务写 documents + chunks → commit。
  ⑦ 若 ⑥ 失败：事务回滚 + 按前缀 delete_prefix 清掉 ⑤ 刚写的对象（补偿，幂等）。

★ 为什么先写对象存储、后写库：对象 key 只依赖预生成的 doc_uuid，与 DB id 解耦，
  DB 提交失败时可按前缀整段补偿删除；
  反过来（先写库、后写对象）一旦对象写入失败，就会残留指向不存在对象的 documents 行，
  更难清理，且检索时会命中"没有原文的引用卡片"。

★ 为什么 status 只在成功时写 done：状态机推进（pending→processing→done）与
  失败原因（error_reason）落库属 T19，本任务不做。

★ 在线/离线隔离（R10）：本函数是纯编排逻辑，必须由 worker 调用，
  **禁止**在 HTTP 请求里同步执行——解析 + 向量化耗时远超 3 秒（T18/T22 接队列）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.embedding import embed_texts
from app.core.storage import (
    delete_prefix,
    ensure_bucket,
    parsed_key,
    put_text,
    raw_key,
)
from app.repos.chunk_repo import ChunkInsert, insert_chunks
from app.repos.document_repo import insert_document
from app.services.chunker import chunk_text
from app.services.file_validator import ensure_not_duplicate, validate_metadata
from app.services.md_parser import parse_markdown

# 分块策略（与 app/services/chunker.py 默认值、V-1 脚本一致）
CHUNK_SIZE = 512
CHUNK_OVERLAP = 64

# 原文对象的 Content-Type 按来源类型区分（与 scripts/store_file.py 同约定）
_RAW_CONTENT_TYPE = {
    "markdown": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}


class IngestError(RuntimeError):
    """入库编排期的显式失败（R7：抛出异常，不静默返回半成品）。"""


@dataclass(frozen=True)
class IngestResult:
    """入库结果。chunk_count 与 vector_count 必须相等（T13 验收标准）。"""

    doc_id: int
    doc_uuid: str
    chunk_count: int
    vector_count: int
    raw_key: str
    parsed_key: str


def ingest_document(
    session: Session,
    *,
    kb_id: int,
    filename: str,
    data: bytes,
    scope: str = "internal",
) -> IngestResult:
    """把一份文件走完入库全链路，返回入库结果。

    Args:
        session: 调用方提供的 Session（事务边界由本函数控制，内部会 commit）。
        kb_id: 目标知识库 id。
        filename: 上传文件名（决定 source_type 与原文对象扩展名）。
        data: 文件字节内容。
        scope: 可见范围。V-1 固定 "internal"，scope_key 直接取该值，
            真实推导（用户/文档 → scope_key）属 T14 / T15。

    Raises:
        FileValidationError: 类型 / 大小 / 去重校验失败（T12），含契约错误码。
        IngestError: 非 UTF-8 编码、解析或分块为空、向量数与分块数不一致。
    """
    # ---- ① 校验 + 去重（只读，无写入；尽早失败）----
    validated = validate_metadata(filename, data)
    ensure_not_duplicate(session, kb_id=kb_id, file_md5=validated.file_md5)

    # ---- ② 解析（去页眉页脚）----
    try:
        source_text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        # 用户上传的内容是系统边界：给一句人话，而不是裸露 UnicodeDecodeError
        raise IngestError(
            f"文件不是 UTF-8 编码，无法解析：{filename}（{e}）"
        ) from e
    parsed_text = parse_markdown(source_text).text
    if not parsed_text.strip():
        raise IngestError(f"解析后内容为空，拒绝入库：{filename}")

    # ---- ③ 分块 ----
    chunks = chunk_text(
        parsed_text, chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
    )
    if not chunks:
        raise IngestError(f"分块结果为空，拒绝入库：{filename}")

    # ---- ④ 向量化（调嵌入服务，此刻不持有 DB 事务/行锁）----
    vectors = embed_texts([c.content for c in chunks])
    if len(vectors) != len(chunks):
        # R7：嵌入服务返回值异常必须显式失败，不能按短的截断凑合入库
        raise IngestError(
            f"向量数与分块数不一致：{len(vectors)} != {len(chunks)}（嵌入服务返回异常）"
        )

    # ---- ⑤ 写对象存储（key 由预生成的 doc_uuid 决定，不依赖 DB id）----
    doc_uuid = str(uuid.uuid4())
    key_raw = raw_key(doc_uuid, validated.ext)
    key_parsed = parsed_key(doc_uuid)
    ensure_bucket()  # 幂等；桶默认私有，不设匿名访问
    put_text(key_raw, source_text, content_type=_content_type(validated.source_type))
    put_text(key_parsed, parsed_text)

    # ---- ⑥ 单事务写库；失败则回滚并补偿删除 ⑤ 写的对象 ----
    try:
        doc_id = insert_document(
            session,
            kb_id=kb_id,
            title=validated.title,
            source_type=validated.source_type,
            storage_key=key_raw,
            parsed_key=key_parsed,
            file_md5=validated.file_md5,
            file_size=validated.file_size,
            scope=scope,
            status="done",
        )
        insert_chunks(
            session,
            doc_id=doc_id,
            kb_id=kb_id,
            scope_key=scope,  # V-1：scope_key 由 doc.scope 直接取值（T15 改真实推导）
            items=[
                ChunkInsert(
                    content=c.content,
                    chunk_index=c.chunk_index,
                    char_start=c.char_start,
                    char_end=c.char_end,
                    embedding=vectors[c.chunk_index],
                )
                for c in chunks
            ],
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        try:
            delete_prefix(f"docs/{doc_uuid}/")
        except Exception as cleanup_exc:
            # 补偿失败必须显式上报（R7），且不能盖掉原始错误——链式抛出
            raise IngestError(
                f"入库失败且对象存储补偿清理也失败（doc_uuid={doc_uuid}，"
                f"请人工清理 docs/{doc_uuid}/）：{cleanup_exc}"
            ) from exc
        raise

    return IngestResult(
        doc_id=doc_id,
        doc_uuid=doc_uuid,
        chunk_count=len(chunks),
        vector_count=len(vectors),
        raw_key=key_raw,
        parsed_key=key_parsed,
    )


def _content_type(source_type: str) -> str:
    """source_type → 原文对象的 Content-Type。unknown 类型走纯文本兜底。"""
    return _RAW_CONTENT_TYPE.get(source_type, "text/plain; charset=utf-8")
