"""T33 评估结果入库：eval_repo + 三张表约束的真机集成测试。

验收对齐 docs/tasks.md T33 行：
- "库里多出 1 条 run + 30 条 item，item 里能看到 answer 全文与分段耗时"
  → test_run_with_30_items_round_trip
- 两条 DB 兜底约束（ck_active_requires_confirmed / ck_attribution /
  uq_run_baseline）必须真实拦得住 → 三条负例测试
- "make migrate-down 可回滚" → 手动验收，不进自动化（见 T33 汇报）

依赖：仅 PostgreSQL（不需要 Ollama / silo）。
★ 与 test_ingest.py 同风格：真实 PG、测试数据用 name 前缀隔离、前后各清一次。
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.repos.eval_repo import (
    activate_dataset,
    cleanup_hanging_runs,
    create_run,
    finish_run,
    get_run,
    insert_run_items,
    register_dataset,
    set_baseline,
)
from app.repos.models import EvalDataset, EvalRun, EvalRunItem

# 测试数据集统一前缀，_purge 按它清理，与真实数据集隔离
NAME_PREFIX = "test-eval-repo"


def _pg_available() -> bool:
    if not os.environ.get("DATABASE_URL"):
        return False
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            pass
    except OSError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="PostgreSQL 未启动，跳过评估入库集成测试",
)


def _purge(engine: Engine) -> None:
    """按 name 前缀清掉测试数据集（级联清 runs / items）。"""
    with Session(engine) as session:
        ds_ids = session.scalars(
            select(EvalDataset.id).where(EvalDataset.name.like(f"{NAME_PREFIX}%"))
        ).all()
        if ds_ids:
            run_ids = session.scalars(
                select(EvalRun.id).where(EvalRun.dataset_id.in_(ds_ids))
            ).all()
            if run_ids:
                session.execute(
                    delete(EvalRunItem).where(EvalRunItem.run_id.in_(run_ids))
                )
            session.execute(delete(EvalRun).where(EvalRun.dataset_id.in_(ds_ids)))
            session.execute(delete(EvalDataset).where(EvalDataset.id.in_(ds_ids)))
            session.commit()


@pytest.fixture()
def pg() -> Iterator[Engine]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    _purge(engine)
    yield engine
    _purge(engine)


def _register(pg_engine: Engine, *, name: str, unconfirmed: int = 0) -> int:
    """注册一个测试数据集（30 条四类分布），返回 dataset_id。"""
    with Session(pg_engine) as session:
        return register_dataset(
            session,
            name=name,
            version="1.0.0",
            repo_path="evals/golden_set.yaml",
            file_path=None,
            file_format="yaml",
            checksum="0" * 64,  # sha256 占位：本测试不校验内容，只验入库行为
            item_count=30,
            distribution={"normal": 18, "refuse": 6, "permission": 3, "edge": 3},
            tag_stats={"year-leave": 3},
            unconfirmed_count=unconfirmed,
        )


class TestEvalRepo:
    """T33 验收：三张表 + eval_repo + 约束兜底。"""

    def test_register_and_activate(self, pg: Engine) -> None:
        """注册为 draft，确认完毕（unconfirmed=0）后可激活为 active。"""
        ds_id = _register(pg, name=f"{NAME_PREFIX}-basic")

        with Session(pg) as session:
            row = session.get(EvalDataset, ds_id)
            assert row is not None
            assert row.state == "draft", "注册后必须是 draft，不能直接 active"
            assert row.checksum == "0" * 64

            activate_dataset(session, ds_id)
            assert session.get(EvalDataset, ds_id).state == "active"  # type: ignore[union-attr]

    def test_activate_blocked_when_unconfirmed(self, pg: Engine) -> None:
        """ck_active_requires_confirmed：还有未确认条目的数据集禁止启用（R13/R16）。"""
        ds_id = _register(pg, name=f"{NAME_PREFIX}-unconfirmed", unconfirmed=7)

        with Session(pg) as session, pytest.raises(IntegrityError):
            activate_dataset(session, ds_id)

    def test_run_with_30_items_round_trip(self, pg: Engine) -> None:
        """T33 验收核心：1 条 run + 30 条 item，item 里能读回 answer 全文与分段耗时。"""
        ds_id = _register(pg, name=f"{NAME_PREFIX}-run30")

        with Session(pg) as session:
            run_id = create_run(
                session,
                dataset_id=ds_id,
                git_commit="c2d7a7e",
                config_snapshot={"prompt": "answer_v1", "chunk": "512/64", "top_k": 5},
                model_config_ids=[1, 2],
            )

        items = [
            EvalRunItem(
                run_id=run_id,
                item_id=f"Q{i:03d}",
                category="normal",
                question=f"第 {i} 道评估题",
                status="pass",
                answer=f"第 {i} 题的系统实际输出全文" * 10,
                refused=False,
                latency_ms=2400 + i,
                retrieve_ms=380 + i,
                generate_ms=2000 + i,
                cost_cny=Decimal("0.0210"),
                attribution_candidate="unclassified",
            )
            for i in range(1, 31)
        ]
        with Session(pg) as session:
            insert_run_items(session, run_id, items)
            session.commit()

            finish_run(
                session,
                run_id,
                status="done",
                metrics={
                    "retrieval": {"recall@5": 0.9},
                    "e2e": {"task_completion": 0.93},
                },
                cost_cny=Decimal("0.6300"),
                wall_time_s=720,
            )

        with Session(pg) as session:
            run = get_run(session, run_id)
            assert run.status == "done"
            assert run.metrics["retrieval"]["recall@5"] == 0.9  # type: ignore[index]
            assert run.wall_time_s == 720

            count = session.scalar(
                select(func.count())
                .select_from(EvalRunItem)
                .where(EvalRunItem.run_id == run_id)
            )
            assert count == 30, "必须恰好 30 条 item"

            q001 = session.execute(
                select(EvalRunItem).where(
                    EvalRunItem.run_id == run_id, EvalRunItem.item_id == "Q001"
                )
            ).scalar_one()
            assert q001.answer is not None and len(q001.answer) > 50, (
                "answer 全文必须落库"
            )
            assert q001.retrieve_ms is not None and q001.generate_ms is not None, (
                "分段耗时 retrieve_ms / generate_ms 必须落库"
            )

    def test_second_baseline_rejected(self, pg: Engine) -> None:
        """uq_run_baseline：同一数据集只允许一个基线 run。"""
        ds_id = _register(pg, name=f"{NAME_PREFIX}-baseline")

        with Session(pg) as session:
            run_a = create_run(
                session, dataset_id=ds_id, git_commit="a" * 7,
                config_snapshot={}, model_config_ids=None,
            )
            run_b = create_run(
                session, dataset_id=ds_id, git_commit="b" * 7,
                config_snapshot={}, model_config_ids=None,
            )
            set_baseline(session, run_a)
            with pytest.raises(IntegrityError):
                set_baseline(session, run_b)

    def test_attribution_confirmed_requires_evidence(self, pg: Engine) -> None:
        """ck_attribution：归因结论必须与 candidate + confirmed_by 并存（R15）。"""
        ds_id = _register(pg, name=f"{NAME_PREFIX}-attr")

        with Session(pg) as session:
            run_id = create_run(
                session, dataset_id=ds_id, git_commit="c" * 7,
                config_snapshot={}, model_config_ids=None,
            )
            bad = EvalRunItem(
                run_id=run_id,
                item_id="Q001",
                category="normal",
                question="题",
                status="fail",
                # 只有结论、没有 candidate 与 confirmed_by —— 必须被约束拦下
                attribution_confirmed="retrieval",
            )
            with pytest.raises(IntegrityError):
                insert_run_items(session, run_id, [bad])
                session.flush()


class TestCleanupHangingRuns:
    """T28b：硬中断残留的僵尸 run → 下次启动时批量标 failed。

    成因已证实：Python 层异常 main() 的 except 会正确落 failed 并 commit
    （实验：boom_chain 抛异常后 run 40 持久化 failed）；只有 OS 层硬杀
    才会让 run 永久停在 running。
    """

    def test_cleans_old_running_runs(self, pg: Engine) -> None:
        """running + started_at 很久以前 → 批量标 failed，备注写 cleanup。"""
        ds_id = _register(pg, name=f"{NAME_PREFIX}-cleanup-old")
        with Session(pg) as session:
            r1 = create_run(session, dataset_id=ds_id, git_commit="a" * 7,
                            config_snapshot={}, model_config_ids=None)
            r2 = create_run(session, dataset_id=ds_id, git_commit="b" * 7,
                            config_snapshot={}, model_config_ids=None)
            # 造僵尸：手动把两个 run 的 started_at 改成 30 分钟前（超过阈值）
            now = datetime.now(UTC)
            session.execute(
                select(EvalRun).where(EvalRun.id.in_([r1, r2]))
            ).scalars().all()
            for run_id in (r1, r2):
                r = session.get(EvalRun, run_id)
                assert r is not None
                r.started_at = now - timedelta(minutes=55)  # 超过 50min 阈值
            session.commit()

        # 调用清理（显式 50 分钟阈值，与 runner 入口一致）
        with Session(pg) as session:
            n = cleanup_hanging_runs(session, max_age_minutes=50)
            assert n == 2

        with Session(pg) as session:
            for run_id in (r1, r2):
                r = session.get(EvalRun, run_id)
                assert r is not None
                assert r.status == "failed"
                assert r.error_reason is not None and "cleanup" in r.error_reason
                assert r.finished_at is not None

    def test_young_running_runs_preserved(self, pg: Engine) -> None:
        """running 但 started_at 很近（未到阈值） → 不动它。"""
        ds_id = _register(pg, name=f"{NAME_PREFIX}-cleanup-young")
        with Session(pg) as session:
            r = create_run(session, dataset_id=ds_id, git_commit="a" * 7,
                           config_snapshot={}, model_config_ids=None)
        # 刚建的 run started_at 默认就是当前时间，在阈值内
        with Session(pg) as session:
            n = cleanup_hanging_runs(session, max_age_minutes=50)
            assert n == 0, "近 50 分钟的 running 不应被清"
            assert session.get(EvalRun, r).status == "running"  # type: ignore[union-attr]

    def test_done_and_failed_not_touched(self, pg: Engine) -> None:
        """done / failed 状态的 run 不被 cleanup 碰。"""
        ds_id = _register(pg, name=f"{NAME_PREFIX}-cleanup-done")
        with Session(pg) as session:
            run_id = create_run(session, dataset_id=ds_id, git_commit="a" * 7,
                                 config_snapshot={}, model_config_ids=None)
            finish_run(session, run_id, status="done",
                       metrics={"ok": True}, wall_time_s=42)
        # 造 old 但 status=done
        with Session(pg) as session:
            r = session.get(EvalRun, run_id)
            assert r is not None
            r.started_at = datetime.now(UTC) - timedelta(minutes=120)
            session.commit()
        with Session(pg) as session:
            n = cleanup_hanging_runs(session, max_age_minutes=50)
            assert n == 0
            r = session.get(EvalRun, run_id)
            assert r is not None and r.status == "done", "done run 不能被改状态"

    def test_zero_cleanup_when_nothing_hanging(self, pg: Engine) -> None:
        """没有僵尸 → 返回 0（幂等，下次启动调一次也没问题）。"""
        _register(pg, name=f"{NAME_PREFIX}-cleanup-empty")
        with Session(pg) as session:
            n = cleanup_hanging_runs(session, max_age_minutes=50)
            assert n == 0
