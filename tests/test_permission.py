"""T14 权限解析测试：文档 → scope_key、用户 → 可见范围集合。

两类：
- 单元（不需要 DB）：`document_scope_key` / `user_scope_keys` —— 纯函数，
  覆盖白名单校验与聚合规则（验收："给定用户与文档，能算出正确的 scope_key"）。
- 集成（需要 PostgreSQL）：`resolve_scope_keys` —— 覆盖真实查库，
  以及"用户不存在 / 已禁用必须显式报错"（R7）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.repos.models import User, UserScopeGrant
from app.repos.user_repo import find_user_by_username
from app.services.permission import (
    ScopeError,
    document_scope_key,
    resolve_scope_keys,
    user_scope_keys,
)


def _user(dept: str | None) -> User:
    """构造一个未入库的 User 对象（纯函数测试用）。"""
    return User(
        username="u",
        display_name="U",
        dept=dept,
        role="user",
        status="active",
    )


class TestDocumentScopeKey:
    """文档侧推导：documents.scope → chunks.scope_key。"""

    @pytest.mark.parametrize(
        ("scope", "expected"),
        [
            ("public", "public"),
            ("internal", "internal"),
            ("dept:研发部", "dept:研发部"),
            ("  internal  ", "internal"),
            ("dept: 销售部 ", "dept:销售部"),  # 内部空白被规整成同一个 key
        ],
    )
    def test_valid_scope(self, scope: str, expected: str) -> None:
        assert document_scope_key(scope) == expected

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "dept:", "dept:   ", "PUBLIC", "all", "dept", "kb:3"],
    )
    def test_invalid_scope_raises(self, bad: str) -> None:
        """非法取值必须显式报错，不能兜底成 internal（否则等于静默"全员可见"）。"""
        with pytest.raises(ScopeError):
            document_scope_key(bad)


class TestUserScopeKeys:
    """用户侧聚合：{public, internal} ∪ {dept:<用户部门>} ∪ 授权表。"""

    def test_base_and_dept(self) -> None:
        assert user_scope_keys(_user("销售部"), []) == [
            "dept:销售部",
            "internal",
            "public",
        ]

    def test_no_dept_only_base(self) -> None:
        assert user_scope_keys(_user(None), []) == ["internal", "public"]

    def test_grants_are_unioned_deduped_and_sorted(self) -> None:
        keys = user_scope_keys(_user("研发部"), ["dept:销售部", "kb:3", "internal"])
        # kb: 形式原样并入（data-model 2.5 认可），不因白名单被拒
        assert set(keys) == {
            "dept:研发部",
            "dept:销售部",
            "kb:3",
            "internal",
            "public",
        }
        # 排序稳定 → 缓存 fingerprint 稳定（R3）
        assert keys == sorted(keys)

    def test_blank_grants_ignored(self) -> None:
        assert user_scope_keys(_user(None), ["", "   "]) == ["internal", "public"]

    def test_kb_grant_warns_but_stays_fail_closed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """★ 已知缺口护栏：`kb:` 授权暂不生效（kb_id 轴未实现，见 tasks.md）。

        断言两件事：
        - 行为不变：授权值原样并入返回。它参与 scope_key 过滤但 chunks.scope_key
          里没有 `kb:` 形式的值 → 匹配不到任何内容 → fail-closed，**不越权**；
        - 缺口可见：打一条 `event=permission, status=warn` 结构化日志（R7）——
          静默的话，多知识库场景上线时这就是跨库泄露。
        """
        result = user_scope_keys(_user("销售部"), ["kb:3"])
        assert "kb:3" in result

        err = capsys.readouterr().err
        events = [json.loads(ln) for ln in err.splitlines() if ln.startswith("{")]
        warns = [e for e in events if e.get("event") == "permission"]
        assert warns, "出现 kb: 授权时必须打 WARN 日志"
        assert warns[-1]["status"] == "warn"
        assert warns[-1]["values"] == ["kb:3"]

    def test_no_kb_grant_no_warn(self, capsys: pytest.CaptureFixture[str]) -> None:
        """没有 `kb:` 授权时不打 WARN（避免日志噪音）。"""
        user_scope_keys(_user("销售部"), ["dept:研发部"])
        err = capsys.readouterr().err
        assert '"event": "permission"' not in err


def _db_available() -> bool:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return False
    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception:
        return False
    return True


# 专用测试账号名：避免与 scripts/seed_users.py 的演示账号 / 真实数据混淆
USERNAME = "t14_test_user"


def _purge(engine: Engine) -> None:
    """清掉测试账号及其授权，保证可重复跑。"""
    with Session(engine) as session:
        user = find_user_by_username(session, username=USERNAME)
        if user is not None:
            session.execute(
                delete(UserScopeGrant).where(UserScopeGrant.user_id == user.id)
            )
            session.execute(delete(User).where(User.id == user.id))
            session.commit()


@pytest.fixture()
def engine() -> Iterator[Engine]:
    if not _db_available():
        pytest.skip("PostgreSQL 未启动，跳过权限解析集成测试")
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    _purge(eng)
    yield eng
    _purge(eng)


class TestResolveScopeKeys:
    """端到端：按用户名解析可见范围，并据此判断某文档是否可见。"""

    def _seed(self, engine: Engine, *, status: str = "active") -> None:
        with Session(engine) as session:
            user = User(
                username=USERNAME,
                display_name="T14 测试用户",
                dept="销售部",
                role="user",
                status=status,
            )
            session.add(user)
            session.flush()
            session.add(UserScopeGrant(user_id=user.id, scope_key="dept:研发部"))
            session.commit()

    def test_resolves_base_dept_and_grants(self, engine: Engine) -> None:
        self._seed(engine)
        with Session(engine) as session:
            keys = resolve_scope_keys(session, username=USERNAME)

        assert set(keys) == {
            "public",
            "internal",
            "dept:销售部",  # 来自 users.dept
            "dept:研发部",  # 来自授权表（跨部门项目组）
        }

        # ★ 验收：给定用户与文档，能算出正确的 scope_key 并判断是否可见
        assert document_scope_key("dept:销售部") in keys
        assert document_scope_key("internal") in keys
        assert document_scope_key("dept:财务部") not in keys

    def test_unknown_user_raises(self, engine: Engine) -> None:
        with Session(engine) as session:
            with pytest.raises(ScopeError, match="用户不存在"):
                resolve_scope_keys(session, username=USERNAME)

    def test_disabled_user_raises(self, engine: Engine) -> None:
        self._seed(engine, status="disabled")
        with Session(engine) as session:
            with pytest.raises(ScopeError, match="用户已禁用"):
                resolve_scope_keys(session, username=USERNAME)

    def test_grants_query_returns_scope_keys(self, engine: Engine) -> None:
        """查库路径本身：user_repo.list_scope_keys 能取到授权值。"""
        self._seed(engine)
        with Session(engine) as session:
            user = find_user_by_username(session, username=USERNAME)
            assert user is not None
            rows = session.scalars(
                select(UserScopeGrant.scope_key).where(
                    UserScopeGrant.user_id == user.id
                )
            ).all()
        assert list(rows) == ["dept:研发部"]
