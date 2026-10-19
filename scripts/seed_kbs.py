"""V0.1 T18b：给 dev 库存量 kb 1/2/3 补 knowledge_bases 数据行。

背景：knowledge_bases 表由 T21（迁移 0004）才建；此前 dev 库里 documents 已有
kb_id=1/2/3 的存量文档，却没有对应的知识库行。T18b 把上传接口接上"知识库默认
scope"后，kb 必须真实存在（不存在 → 404），因此先补齐这三行。

用法：
    python scripts/seed_kbs.py          # 幂等灌数据：已存在的 kb 不重复插入
    python scripts/seed_kbs.py --list    # 只读：打印现有知识库，不写入

★ 为什么用脚本而不是迁移：这三行是 **dev 存量数据治理**，不是所有环境都该有；
  写进 Alembic 迁移会在全新/生产库凭空造出演示知识库（R8：数据治理与结构变更
  分开做）。
★ 为什么必须显式指定 id：documents.kb_id=1/2/3 已存在，补的行必须与之对齐，
  否则 FK/归属关系对不上。
★ 为什么插入后要 setval：显式 id 插入**不推进**序列；若不把序列调到 MAX(id)
  以上，之后用 create_kb 新建知识库时 nextval() 可能返回已被占用的 id → 主键冲突。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.repos.kb_repo import find_kb_by_name  # noqa: E402
from app.repos.models import KnowledgeBase  # noqa: E402

# dev 存量 kb：按现有文档内容命名、按其可见范围定 default_scope
# （2026-10-19 与项目所有者确认）。
#   kb 1：年假/绩效/萤火计划等演示样例 → 通用演示库，internal
#   kb 2/3：各含一份《研发部内部制度》→ 研发部库，dept:研发部
SEED_KBS = [
    (1, "演示知识库", "internal"),
    (2, "研发部知识库", "dept:研发部"),
    (3, "研发部知识库二", "dept:研发部"),
]


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


def _print_kbs(session: Session) -> None:
    print("── 现有知识库 ──", file=sys.stderr)
    rows = session.execute(
        select(KnowledgeBase.id, KnowledgeBase.name, KnowledgeBase.default_scope)
        .order_by(KnowledgeBase.id)
    ).all()
    for kb_id, name, default_scope in rows:
        print(
            f"  id={kb_id}  name={name}  default_scope={default_scope}",
            file=sys.stderr,
        )
    print(f"  共 {len(rows)} 个", file=sys.stderr)


def _upsert_kb(
    session: Session, *, kb_id: int, name: str, default_scope: str
) -> None:
    """按 id 幂等插入知识库；已存在则跳过。

    ★ 名字唯一约束也可能撞车（该 name 已挂在别的 id 上）：此时跳过并提示，
      绝不为了补齐去改别人的行（数据治理要保守，只增不改）。
    """
    existing = session.get(KnowledgeBase, kb_id)
    if existing is not None:
        print(
            f"已存在，跳过 kb：id={kb_id}（当前 name={existing.name}）",
            file=sys.stderr,
        )
        return
    if find_kb_by_name(session, name=name) is not None:
        print(
            f"名称已被占用，跳过 kb：id={kb_id} name={name}（已挂在别的 id 上）",
            file=sys.stderr,
        )
        return
    session.add(
        KnowledgeBase(
            id=kb_id,
            name=name,
            default_scope=default_scope,
            created_by=None,
        )
    )
    session.flush()
    print(
        f"新增知识库：id={kb_id} name={name} default_scope={default_scope}",
        file=sys.stderr,
    )


def _advance_sequence(session: Session) -> None:
    """把 knowledge_bases 自增序列调到 MAX(id) 以上（幂等，只升不降）。

    GREATEST 同时兜住"序列已推进过、比表里 MAX(id) 还大"的情况，避免回退造成
    之后新建知识库时主键冲突。
    """
    session.execute(
        text(
            "SELECT setval("
            "  pg_get_serial_sequence('knowledge_bases', 'id'),"
            "  (SELECT GREATEST("
            "     COALESCE(MAX(id), 1),"
            "     (SELECT last_value FROM pg_sequences"
            "      WHERE sequencename = 'knowledge_bases_id_seq')"
            "   ) FROM knowledge_bases)"
            ")"
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="V0.1 T18b dev 存量知识库维护脚本")
    parser.add_argument(
        "--list", action="store_true", help="只读：打印现有知识库，不写入"
    )
    args = parser.parse_args()

    env = load_env()
    if not env.get("DATABASE_URL"):
        sys.exit("DATABASE_URL 未设置：请在项目根 .env 中配置（cp .env.example .env）")
    os.environ.update(env)

    engine = create_engine(env["DATABASE_URL"])
    with Session(engine) as session:
        if args.list:
            _print_kbs(session)
            return

        for kb_id, name, default_scope in SEED_KBS:
            _upsert_kb(session, kb_id=kb_id, name=name, default_scope=default_scope)
        session.flush()
        _advance_sequence(session)
        session.commit()

        _print_kbs(session)


if __name__ == "__main__":
    main()
