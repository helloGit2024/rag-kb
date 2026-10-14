"""documents 表数据访问（V0.1 · T12 / T13）。

★ app/repos/ 是唯一允许出现 SQL 的层（目录约定）。
本模块提供 md5 去重查询（T12）与文档插入（T13）；其余文档 CRUD 在 T21 / T22 扩展。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.repos.models import Document


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
) -> int:
    """插入一条 documents 记录并 flush（拿到自增 id），返回新文档 id。

    ★ 不 commit：事务边界由调用方（ingest 编排）统一控制——
    documents 与 chunks 必须同生共死，绝不能各自提交（T13 验收"中途失败能回滚"）。

    ★ status 由调用方显式传入、不给默认值：写状态是编排层的决定，
    T13 只在整条链路成功时传 "done"，状态机推进属 T19。
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
    )
    session.add(doc)
    session.flush()
    return doc.id
