"""users / user_scope_grants 表数据访问（V0.1 · T14）。

★ app/repos/ 是唯一允许出现 SQL 的层（目录约定）。
本模块只提供"按用户名查用户"与"查某用户的授权列表"两个查询；
权限的**判定逻辑**（怎么把用户与授权聚合成可见范围）在
`app/services/permission.py`，不在这里（目录约定：repos 只做数据访问）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.repos.models import User, UserScopeGrant


def find_user_by_username(session: Session, *, username: str) -> User | None:
    """按登录名查用户。

    返回 None 表示查无此人；是否报错由 service 层决定（R7）。
    """
    return session.scalar(select(User).where(User.username == username))


def list_scope_keys(session: Session, *, user_id: int) -> list[str]:
    """列出某用户被授予的全部 scope_key（按 id 稳定排序，便于结果可复现）。"""
    rows = session.scalars(
        select(UserScopeGrant.scope_key)
        .where(UserScopeGrant.user_id == user_id)
        .order_by(UserScopeGrant.id)
    ).all()
    return list(rows)
