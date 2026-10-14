"""V0.1 T14：灌入测试用户与授权（V0.1 无管理界面，靠脚本维护）。

用法：
    python scripts/seed_users.py          # 幂等灌数据：已存在的用户/授权不重复插入
    python scripts/seed_users.py --list   # 只读：打印现有用户与其可见范围

★ 为什么用脚本而不是 .sql 文件：与 scripts/ 下其他脚本同约定，且可直接复用
  app/services/permission.py 的解析逻辑，把"这个用户到底能看到什么"打印出来，
  便于你验收（给定用户与文档能否算出正确的 scope_key）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.repos.models import User, UserScopeGrant  # noqa: E402
from app.repos.user_repo import find_user_by_username  # noqa: E402
from app.services.permission import resolve_scope_keys  # noqa: E402

# 演示用账号（username, display_name, dept, role）
TEST_USERS = [
    ("admin", "管理员", "管理部", "admin"),
    ("alice", "爱丽丝", "销售部", "user"),
    ("bob", "鲍勃", "研发部", "user"),
]
# 额外授权：bob 在跨部门项目组里，故额外可见销售部
TEST_GRANTS = {"bob": ["dept:销售部"]}


def load_env() -> dict[str, str]:
    """读项目根 .env，并把 compose 服务名 postgres 替换为 localhost。"""
    env: dict[str, str] = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    if "@postgres:" in env.get("DATABASE_URL", ""):
        env["DATABASE_URL"] = env["DATABASE_URL"].replace("@postgres:", "@localhost:")
    return env


def _upsert_user(
    session: Session, *, username: str, display_name: str, dept: str, role: str
) -> User:
    """按 username 幂等插入用户；已存在则原样返回（不覆盖人工改过的数据）。"""
    user = find_user_by_username(session, username=username)
    if user is not None:
        print(f"已存在，跳过用户：{username}", file=sys.stderr)
        return user
    user = User(
        username=username,
        display_name=display_name,
        dept=dept,
        role=role,
        status="active",
    )
    session.add(user)
    session.flush()
    print(f"新增用户：{username}（dept={dept}, role={role}）", file=sys.stderr)
    return user


def _upsert_grant(session: Session, *, user_id: int, scope_key: str) -> None:
    """幂等插入授权（授权表无唯一约束，故先查后插）。"""
    existing = session.scalar(
        select(UserScopeGrant.id)
        .where(
            UserScopeGrant.user_id == user_id,
            UserScopeGrant.scope_key == scope_key,
        )
        .limit(1)
    )
    if existing is not None:
        print(f"已存在，跳过授权：user_id={user_id} ← {scope_key}", file=sys.stderr)
        return
    session.add(UserScopeGrant(user_id=user_id, scope_key=scope_key))
    print(f"新增授权：user_id={user_id} ← {scope_key}", file=sys.stderr)


def _print_scope_table(session: Session, usernames: list[str]) -> None:
    print("── 用户可见范围（resolve_scope_keys 的实际输出）──", file=sys.stderr)
    for username in usernames:
        keys = resolve_scope_keys(session, username=username)
        print(f"  {username}: {keys}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="V0.1 T14 测试用户/授权维护脚本")
    parser.add_argument(
        "--list", action="store_true", help="只读：打印现有用户与其可见范围，不写入"
    )
    args = parser.parse_args()

    env = load_env()
    if not env.get("DATABASE_URL"):
        sys.exit("DATABASE_URL 未设置：请在项目根 .env 中配置（cp .env.example .env）")
    os.environ.update(env)

    engine = create_engine(env["DATABASE_URL"])
    with Session(engine) as session:
        if args.list:
            users = session.scalars(select(User.username).order_by(User.id)).all()
            _print_scope_table(session, list(users))
            return

        for username, display_name, dept, role in TEST_USERS:
            user = _upsert_user(
                session,
                username=username,
                display_name=display_name,
                dept=dept,
                role=role,
            )
            for scope_key in TEST_GRANTS.get(username, []):
                _upsert_grant(session, user_id=user.id, scope_key=scope_key)
        session.commit()

        _print_scope_table(session, [u[0] for u in TEST_USERS])


if __name__ == "__main__":
    main()
