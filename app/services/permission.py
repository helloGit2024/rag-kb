"""权限解析：文档 → scope_key，用户 → 可见范围集合（V0.1 · T14 / T17b）。

职责边界（与 T15 的分工）：
- 本模块（写入侧 + 解析逻辑）：
    · `document_scope_key()` —— 由 `documents.scope` 推导出 chunks 的 `scope_key`；
    · `user_scope_keys()` —— 把"用户 + 授权记录"聚合成**非 kb 的**可见范围集合
      （kb: 授权剥离到 kb_ids 处理，见下）；
    · `user_accessible_kb_ids()` —— 从 kb: 授权里解析出 int 列表（kb_id 轴）；
    · `resolve_permission()` —— 一次查库同时拿到 scope_keys + kb_ids（T17b 新增）；
    · `resolve_scope_keys()` / `resolve_kb_ids()` —— 只取其中一份（向后兼容）；
    · `scope_fingerprint()` —— 把可见范围压成缓存 key 用的稳定指纹（R3）。
- T15（检索侧）：把这套范围接到 `search_chunks` 的两条件 SQL 上
  （`scope_key = ANY(...) AND kb_id = ANY(...)`，见 data-model 第三节）。

★ 为什么 kb 可见性不折进 scope_key：data-model 第三节给的**正确写法**就是两条件，
  不是一个条件；scope_key 维度（public / internal / dept:X）和 kb_id 维度是独立的。

T17b 做了什么（相对 T14）：
  1. 新增 `PermissionBundle` + `user_accessible_kb_ids()` + `resolve_permission()`；
  2. `user_scope_keys()` **过滤掉** `kb:` 值（kb 条件独立处理，
     scope_key 里留会永远匹配不上）；
  3. 删掉 `_warn_unimplemented_kb_axis`（kb 授权不再是已知缺口，T17b 实现了）；
  4. `scope_fingerprint()` 扩：kb_ids 非空时参与哈希（R3：缓存 key 含全量权限指纹）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.repos.models import User
from app.repos.user_repo import find_user_by_username, list_scope_keys

SCOPE_PUBLIC = "public"
SCOPE_INTERNAL = "internal"
_DEPT_PREFIX = "dept:"
_KB_PREFIX = "kb:"

# 全员可见的范围：不放进 user_scope_grants 重复维护（已与项目所有者确认）
_ALWAYS_VISIBLE = (SCOPE_PUBLIC, SCOPE_INTERNAL)

# 用户状态取值（docs/data-model.md 2.1）
USER_STATUS_ACTIVE = "active"


class ScopeError(ValueError):
    """scope 值非法或身份无法解析（R7：显式失败，不兜底成"可见"）。"""


@dataclass
class PermissionBundle:
    """一次查库得到的完整权限包（scope_keys + kb_ids，两轴独立）。

    为什么不直接返回 tuple：tuple 只能靠位置记忆哪个是 scope 哪个是 kb，
    调用链一长就容易传反。用 dataclass 的好处是调用处 `.scope_keys` / `.kb_ids`
    自带语义，且默认值让新增字段时不会炸调用方。
    """

    scope_keys: list[str] = field(default_factory=list)
    kb_ids: list[int] = field(default_factory=list)
    # 当前用户主键（T21 起用于记录"谁创建的知识库"）。
    # 留默认值 None：老调用方（测试里的 PermissionBundle(scope_keys=[...])）
    # 不传也不会炸（dataclass 默认值的意义就在这）。
    user_id: int | None = None


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
    """把"用户 + 授权记录"聚合成**非 kb 的**可见范围集合。

    构成：{public, internal} ∪ {"dept:<user.dept>"} ∪ 授权表里的非 kb 授权。

    ★ kb: 授权**不进这里**（T17b），单独交给 `user_accessible_kb_ids()` 解析。
      原因：检索 SQL 里 kb 维度是独立的 `c.kb_id = ANY(...)` 条件，
      和 scope_key 维度无关。把 kb: 值留在这里会让 scope_key = ANY(...) 里
      混入永远匹配不上的值，语义变脏，且让缓存指纹无法区分"只是多了 kb 授权"
      这个维度。
    """
    keys: set[str] = set(_ALWAYS_VISIBLE)
    dept = (user.dept or "").strip()
    if dept:
        keys.add(f"{_DEPT_PREFIX}{dept}")
    for grant in grants:
        value = (grant or "").strip()
        if value and not value.startswith(_KB_PREFIX):
            keys.add(value)
    # 排序稳定 → 缓存 fingerprint 稳定（R3）
    return sorted(keys)


def user_accessible_kb_ids(user: User, grants: Sequence[str]) -> list[int]:
    """从 kb: 授权值里解析出 int 列表（T17b）。

    授权值形如 `kb:3` → 返回 `[3]`。非法格式（`kb:abc`、空字符串、重复）
    一律跳过并去重排序。

    为什么非法格式不抛错：kb: 授权是 data-model 2.5 认可的合法入口，
    个别脏数据只该跳过，不该让整个权限解析失败（R7 显式失败 ≠ 硬炸所有路径）。
    跳过的非法格式由 `resolve_permission` 里的 `capsys` 测试覆盖。
    """
    ids: set[int] = set()
    for grant in grants:
        value = (grant or "").strip()
        if not value.startswith(_KB_PREFIX):
            continue
        suffix = value[len(_KB_PREFIX) :].strip()
        if not suffix:
            continue
        try:
            ids.add(int(suffix))
        except ValueError:
            # 非法格式（kb:abc）：跳过去，不抛错，不上日志——
            # 上游授权表不该写这种值，但在这里挡住就够（fail-closed）。
            continue
    return sorted(ids)


def resolve_scope_keys(session: Session, *, username: str) -> list[str]:
    """按用户名解析 scope_keys（向后兼容；内部调 resolve_permission）。"""
    return resolve_permission(session, username=username).scope_keys


def resolve_kb_ids(session: Session, *, username: str) -> list[int]:
    """按用户名解析 kb_ids（向后兼容；内部调 resolve_permission）。"""
    return resolve_permission(session, username=username).kb_ids


def resolve_permission(
    session: Session, *, username: str
) -> PermissionBundle:
    """按用户名一次查库拿到完整权限包（scope_keys + kb_ids，T17b）。

    Raises:
        ScopeError: 用户不存在或已被禁用（R7：显式失败）。
    """
    user = find_user_by_username(session, username=username)
    if user is None:
        raise ScopeError(f"用户不存在：{username!r}")
    if user.status != USER_STATUS_ACTIVE:
        raise ScopeError(f"用户已禁用：{username!r}（status={user.status!r}）")
    raw_grants = list_scope_keys(session, user_id=user.id)
    return PermissionBundle(
        scope_keys=user_scope_keys(user, raw_grants),
        kb_ids=user_accessible_kb_ids(user, raw_grants),
        user_id=user.id,
    )


def scope_fingerprint(
    scope_keys: Sequence[str], kb_ids: Sequence[int] | None = None
) -> str:
    """把可见范围集合 + kb_ids 压成稳定指纹，供缓存 key 使用（R3）。

    ★ kb_ids 参与哈希（T17b 新增）：否则"销售部+kb:3"与"销售部+kb:5"会命中
    同一缓存 key —— kb 轴越权在缓存层被静默吞掉。

    Raises:
        ScopeError: scope_keys 为空集合（R3/R7）。
    """
    normalized = sorted({k.strip() for k in scope_keys if k and k.strip()})
    if not normalized:
        raise ScopeError("scope_keys 为空，无法生成缓存指纹（R3/R7）")
    payload = "\x1f".join(normalized)
    if kb_ids:
        # kb_ids 非空时拼上，确保两维度都参与哈希
        payload += "\x1fkb:" + ",".join(str(i) for i in sorted(set(kb_ids)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
