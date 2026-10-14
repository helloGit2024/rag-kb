"""documents 表数据访问（V0.1 · T12）。

★ app/repos/ 是唯一允许出现 SQL 的层（目录约定）。
本模块目前只提供 md5 去重查询；文档 CRUD 在 T21 / T22 扩展。
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
