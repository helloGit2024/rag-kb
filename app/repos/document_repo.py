"""documents 表数据访问（V0.1 · T12 / T13 / T22）。

★ app/repos/ 是唯一允许出现 SQL 的层（目录约定）。
本模块提供 md5 去重查询（T12）、文档插入（T13），以及
列表 / 取单个 / 删除（T22）。

★ 权限过滤写在这里的 WHERE 子句（R2），不留给应用层筛（见 `_visibility_conditions`）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, delete, func, select, update
from sqlalchemy.orm import Session

from app.repos.models import Chunk, Document, User

# 契约允许的 status 过滤值（openapi Document.status 六态）。
# ★ T22 定档：列表的 status 参数严格按契约枚举校验，不顺从实现的真实分布
#   （实际只会出现 done，见 app/services/document.py 的说明）。
DOCUMENT_STATUSES: tuple[str, ...] = (
    "pending",
    "parsing",
    "chunking",
    "embedding",
    "done",
    "failed",
)


@dataclass(frozen=True)
class DocumentRow:
    """列表行：documents 行 + 聚合的 chunk_count + join 出的上传者用户名。

    为什么不直接返回 ORM 对象：与 app/services/kb.py 的 KbSummary 同理——
    返回纯数据后，服务层不必担心"session 关闭后属性过期再触发 SELECT"。
    """

    id: int
    kb_id: int | None
    title: str
    source_type: str
    file_size: int | None
    status: str
    scope: str
    chunk_count: int
    error_reason: str | None
    uploaded_by: str | None
    created_at: datetime


def _visibility_conditions(
    *, scope_keys: Sequence[str], kb_ids: Sequence[int] | None
) -> list[ColumnElement[bool]]:
    """可见性过滤条件（R2：下沉到 SQL，不在应用层筛）。

    - scope 轴用 `documents.scope`：它与 `chunks.scope_key` 同值
      （ingest 里由同一个 `document_scope_key()` 推导后分别写入）。
    - kb 轴与 `chunk_repo.search_chunks` **同口径**：kb_ids 为空表示
      "该用户没有 kb: 授权"，此时不追加 kb 条件（V0.1 里 kb: 是叠加维度）。
      两边必须一致，否则会出现"列表里看得见、检索却命中不了"的错觉。
    """
    conditions: list[ColumnElement[bool]] = [
        Document.scope.in_(list(scope_keys))
    ]
    if kb_ids:
        conditions.append(Document.kb_id.in_(list(kb_ids)))
    return conditions


def list_documents(
    session: Session,
    *,
    scope_keys: Sequence[str],
    kb_ids: Sequence[int] | None = None,
    kb_id: int | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[DocumentRow], int]:
    """分页列出文档（只含 `is_active=true`），返回 (rows, total)。

    chunk_count 用**聚合子查询 + LEFT JOIN** 取，避免每行再发一次
    SELECT（N+1）；`coalesce(..., 0)` 让没有块的文档返回 0 而不是 NULL。
    uploaded_by 用 LEFT JOIN users 取 username——契约里该字段是 string，
    而库里存的是 users.id（见 openapi Document schema）。
    """
    conditions: list[ColumnElement[bool]] = [
        Document.is_active.is_(True),
        *_visibility_conditions(scope_keys=scope_keys, kb_ids=kb_ids),
    ]
    if kb_id is not None:
        conditions.append(Document.kb_id == kb_id)
    if status is not None:
        conditions.append(Document.status == status)

    chunk_count_sq = (
        select(
            Chunk.doc_id.label("doc_id"),
            func.count(Chunk.id).label("chunk_count"),
        )
        .group_by(Chunk.doc_id)
        .subquery()
    )
    rows = session.execute(
        select(
            Document.id,
            Document.kb_id,
            Document.title,
            Document.source_type,
            Document.file_size,
            Document.status,
            Document.scope,
            func.coalesce(chunk_count_sq.c.chunk_count, 0),
            Document.error_reason,
            User.username,
            Document.created_at,
        )
        .outerjoin(chunk_count_sq, chunk_count_sq.c.doc_id == Document.id)
        .outerjoin(User, User.id == Document.uploaded_by)
        .where(*conditions)
        .order_by(Document.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    total = int(
        session.scalar(
            select(func.count()).select_from(Document).where(*conditions)
        )
        or 0
    )
    return (
        [
            DocumentRow(
                id=row[0],
                kb_id=row[1],
                title=row[2],
                source_type=row[3],
                file_size=row[4],
                status=row[5],
                scope=row[6],
                chunk_count=int(row[7]),
                error_reason=row[8],
                uploaded_by=row[9],
                created_at=row[10],
            )
            for row in rows
        ],
        total,
    )


def get_active_document(
    session: Session,
    *,
    doc_id: int,
    scope_keys: Sequence[str],
    kb_ids: Sequence[int] | None = None,
) -> Document | None:
    """按 id 取**当前用户可见的、未软删**的文档；否则返回 None。

    ★ 可见性条件同样下沉到 SQL（R2）。T22 定档：删除也要校可见范围，
      不在范围内的文档一律按"不存在"处理（返回 None → 上层 404，
      **不返回 403**）——403 会告诉调用方"这个 id 确实存在"，等于泄露。
    """
    return session.scalar(
        select(Document).where(
            Document.id == doc_id,
            Document.is_active.is_(True),
            *_visibility_conditions(scope_keys=scope_keys, kb_ids=kb_ids),
        )
    )


def delete_chunks_by_doc(session: Session, *, doc_id: int) -> int:
    """真删该文档的全部 chunks（**含向量**），返回删除行数。

    ★ 必须真删而不是标记（data-model 第四节）：只标记的话向量仍在库里，
      任何一条漏了 `is_active` 条件的代码路径都会把它重新检索出来。
    ★ 不 commit：事务边界由服务层控制（与 insert_document 同约定）。
    """
    result = cast(
        "CursorResult[Any]",
        session.execute(delete(Chunk).where(Chunk.doc_id == doc_id)),
    )
    return int(result.rowcount or 0)


def soft_delete_document(session: Session, *, doc_id: int) -> None:
    """软删（is_active=false）。行保留，用于追溯"谁在什么时候删了什么"。"""
    session.execute(
        update(Document).where(Document.id == doc_id).values(is_active=False)
    )
    session.flush()


def list_doc_storage_prefixes(session: Session, *, doc_id: int) -> list[str]:
    """取该文档在对象存储里的前缀（供删除时整段清理，T22）。

    形如 `docs/{doc_uuid}/`（ingest 写入时的约定）。与
    `kb_repo.list_doc_storage_prefixes` 同一口径——只是那里的入参是 kb_id。
    前缀下没有对象时 `delete_prefix` 返回 0，天然幂等。
    """
    row = session.execute(
        select(Document.storage_key, Document.parsed_key).where(
            Document.id == doc_id
        )
    ).first()
    prefixes: set[str] = set()
    for key in row if row else ():
        if key and "/" in key:
            prefixes.add(key.rsplit("/", 1)[0] + "/")
    return sorted(prefixes)


def find_active_by_md5(session: Session, *, kb_id: int, file_md5: str) -> int | None:
    """同 kb 内查找 md5 相同的在用文档 id（去重口径见 docs/data-model.md 2.3）。

    只认 is_active=true：软删除的文档不算重复（数据模型 2.3 的 is_active 语义）。
    返回命中的文档 id；不存在返回 None。
    """
    row = session.execute(
        select(Document.id).where(
            Document.kb_id == kb_id,
            Document.file_md5 == file_md5,
            Document.is_active.is_(True),
        )
    ).first()
    return row[0] if row else None


def insert_document(
    session: Session,
    *,
    kb_id: int,
    title: str,
    source_type: str,
    storage_key: str,
    parsed_key: str,
    file_md5: str,
    file_size: int,
    scope: str,
    status: str,
    uploaded_by: int | None = None,
) -> int:
    """插入一条 documents 记录并 flush（拿到自增 id），返回新文档 id。

    ★ 不 commit：事务边界由调用方（ingest 编排）统一控制——
    documents 与 chunks 必须同生共死，绝不能各自提交（T13 验收"中途失败能回滚"）。

    ★ status 由调用方显式传入、不给默认值：写状态是编排层的决定，
    T13 只在整条链路成功时传 "done"，状态机推进属 T19。

    ★ uploaded_by（T22c，可选）：上传者 users.id，来自任务 payload；
    为 None 表示来源不可追溯（CLI 脚本 / 队列里的旧 payload）——不加默认兜底值。
    """
    doc = Document(
        kb_id=kb_id,
        title=title,
        source_type=source_type,
        storage_key=storage_key,
        parsed_key=parsed_key,
        file_md5=file_md5,
        file_size=file_size,
        status=status,
        scope=scope,
        uploaded_by=uploaded_by,
    )
    session.add(doc)
    session.flush()
    return doc.id
