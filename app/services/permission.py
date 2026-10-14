"""权限解析：文档 → scope_key，用户 → 可见范围集合（V0.1 · T14）。

职责边界（与 T15 的分工）：
- 本模块（写入侧 + 解析逻辑）：
    · `document_scope_key()` —— 由 `documents.scope` 推导出 chunks 的 `scope_key`；
    · `user_scope_keys()` —— 把"用户 + 授权记录"聚合成检索时要用的可见范围集合；
    · `resolve_scope_keys()` —— 按用户名把上面两步串起来。
- T15（检索侧）：把这套范围集合接到 `search_chunks` 的 SQL 条件上
  （`scope_key = ANY(...)` **且** `kb_id = ANY(...)`，见 data-model 第三节），
  并解析 `X-KB-User` header、无权时返回 403（security.md 2.2）。本模块不碰 HTTP。

★ 为什么 kb 可见性不折进 scope_key：data-model 第三节给出的**正确写法**是
  `WHERE c.scope_key = ANY(:scope_keys) AND c.kb_id = ANY(:accessible_kb_ids)`
  —— 两个独立条件。所以 `scope_key` 只表达"文档的可见级别 / 部门"，
  知识库维度由 `kb_id` 承载。（已与项目所有者确认）

★ 与 data-model 2.5 的差异：2.5 的授权示例含 `kb:3`，而本版 `document_scope_key()`
  只生成 public / internal / dept:X。`user_scope_keys()` 对授权值**原样并入、
  不做白名单**（以免拒绝 data-model 认可的 `kb:` 形式）——只是 `kb:3` 要等
  T15 的 kb_id 轴才真正生效。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.repos.models import User
from app.repos.user_repo import find_user_by_username, list_scope_keys

SCOPE_PUBLIC = "public"
SCOPE_INTERNAL = "internal"
_DEPT_PREFIX = "dept:"

# 全员可见的范围：不放进 user_scope_grants 重复维护（已与项目所有者确认）
_ALWAYS_VISIBLE = (SCOPE_PUBLIC, SCOPE_INTERNAL)

# 用户状态取值（docs/data-model.md 2.1）
USER_STATUS_ACTIVE = "active"


class ScopeError(ValueError):
    """scope 值非法或身份无法解析（R7：显式失败，不兜底成"可见"）。"""


def document_scope_key(scope: str) -> str:
    """把 `documents.scope` 推导成 `chunks.scope_key`（V0.1 文档侧推导）。

    合法取值（docs/data-model.md 2.3）：
        - `public` / `internal`
        - `dept:<部门名>`（部门名不能为空；内部空白会被规整掉）

    Raises:
        ScopeError: 取值不在白名单内。**不兜底成 internal**——把配置错误静默成
            "全员可见"是本项目最不可接受的失败形态（宁可显式失败）。
    """
    value = (scope or "").strip()
    if value in _ALWAYS_VISIBLE:
        return value
    if value.startswith(_DEPT_PREFIX):
        dept = value[len(_DEPT_PREFIX) :].strip()
        if dept:
            # 规整内部空白，避免 "dept: 销售部" 与 "dept:销售部" 变成两个不同的 key
            return f"{_DEPT_PREFIX}{dept}"
    raise ScopeError(
        f"scope 取值非法：{scope!r}（仅允许 public / internal / dept:<部门>）"
    )


def user_scope_keys(user: User, grants: Sequence[str]) -> list[str]:
    """把"用户 + 授权记录"聚合成可见范围集合（security.md 2.2：必须是集合）。

    构成（已与项目所有者确认）：
        {public, internal} ∪ {"dept:<user.dept>"} ∪ 授权表里的 scope_key

    - public / internal 视为全员可见，故不在授权表里重复维护；
    - 部门来自 `users.dept`（本部门自动可见）；
    - 授权值原样并入（可含 data-model 2.5 认可的跨部门 `dept:` 与 `kb:` 形式）。

    去重后**排序**返回：同一用户每次得到同一顺序，缓存 key 的指纹才稳定（R3）。
    """
    keys: set[str] = set(_ALWAYS_VISIBLE)
    dept = (user.dept or "").strip()
    if dept:
        keys.add(f"{_DEPT_PREFIX}{dept}")
    for grant in grants:
        value = (grant or "").strip()
        if value:
            keys.add(value)
    return sorted(keys)


def resolve_scope_keys(session: Session, *, username: str) -> list[str]:
    """按用户名解析出该用户的可见范围集合（T15 会把 `X-KB-User` 的值传进来）。

    Raises:
        ScopeError: 用户不存在或已被禁用。**不返回空集合**——空集合会被检索
            当成"无任何可见内容"，而真正的问题是身份没解析出来，必须显式失败（R7）。
    """
    user = find_user_by_username(session, username=username)
    if user is None:
        raise ScopeError(f"用户不存在：{username!r}")
    if user.status != USER_STATUS_ACTIVE:
        raise ScopeError(f"用户已禁用：{username!r}（status={user.status!r}）")
    return user_scope_keys(user, list_scope_keys(session, user_id=user.id))
