"""归因候选生成器测试（app/eval/attribution.py）。

两层：
  1. TestAttributionCandidate——判定表纯函数，无 DB 无模型，
     覆盖 attribution.md 第三节全部规则行 + 优先级 + 边界值；
  2. TestAttributeRunIntegration——真机 PG 集成：checksum 门禁（R14）、
     refuse 类条目回填（无 expected_sources → 不触发检索重跑，
     因此本测试不依赖 Ollama）、R15 边界（绝不写 confirmed）。

集成测试与 test_eval_repo.py 同模式：真机 Postgres，跑前按 name 前缀清场。
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.eval import loader
from app.eval.attribution import (
    GENERATION_SUSPECT,
    RETRIEVAL_SUSPECT,
    UNCLASSIFIED,
    attribute_run,
    attribution_candidate,
)
from app.repos.eval_repo import (
    create_run,
    insert_run_items,
    list_run_items,
    register_dataset,
    update_attribution_candidate,
)
from app.repos.models import EvalDataset, EvalRun, EvalRunItem

NAME_PREFIX = "test-eval-attr"
GOLDEN_SET_PATH = Path("evals/golden_set.yaml")


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
    reason="PostgreSQL 未启动，跳过归因集成测试",
)


def _purge(engine: Engine) -> None:
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


class TestAttributionCandidate:
    """判定表行列级覆盖（attribution.md 第三节，规则顺序即优先级）。"""

    def test_error_status_always_unclassified(self) -> None:
        """规则 1：status=error 先看工程错误，其他条件再匹配也不参与质量归因。"""
        assert (
            attribution_candidate(
                refused=True,
                retrieval_hit=True,  # 单独看会命中规则 3，但 error 优先
                status="error",
                should_refuse=False,
                verdict=None,
                faithfulness=None,
            )
            == UNCLASSIFIED
        )

    def test_should_refuse_but_answered_is_generation_suspect(self) -> None:
        """规则 2（2026-10-22 新增）：该拒未拒。Q027 型——hit 算不出来
        （无 expected_sources），靠 should_refuse 兜住，不掉进 unclassified。"""
        assert (
            attribution_candidate(
                refused=False,
                retrieval_hit=None,
                status="fail",
                should_refuse=True,
                verdict=None,
                faithfulness=None,
            )
            == GENERATION_SUSPECT
        )

    def test_new_rule_beats_hit_based_rules(self) -> None:
        """规则 2 优先级：should_refuse=true 且 refused=false 时，
        即使 hit=False + faithfulness 低（表面像检索问题），仍归生成侧——
        应拒题本来就不该答，答了就是拒答判定的锅。"""
        assert (
            attribution_candidate(
                refused=False,
                retrieval_hit=False,
                status="fail",
                should_refuse=True,
                verdict=None,
                faithfulness=0.3,
            )
            == GENERATION_SUSPECT
        )

    def test_refused_despite_hit_is_generation_suspect(self) -> None:
        """规则 3：资料在手却没答（Q008/Q030 型误拒）。"""
        assert (
            attribution_candidate(
                refused=True,
                retrieval_hit=True,
                status="fail",
                should_refuse=False,
                verdict=None,
                faithfulness=None,
            )
            == GENERATION_SUSPECT
        )

    def test_hard_answer_without_hit_is_retrieval_suspect(self) -> None:
        """规则 4：没找到资料却硬答且忠实度低 → 召回失败叠加幻觉。"""
        assert (
            attribution_candidate(
                refused=False,
                retrieval_hit=False,
                status="fail",
                should_refuse=False,
                verdict=None,
                faithfulness=0.5,
            )
            == RETRIEVAL_SUSPECT
        )

    def test_faithfulness_threshold_boundary(self) -> None:
        """规则 4 边界：faithfulness 恰好 0.85 不算 < 0.85 → unclassified。"""
        assert (
            attribution_candidate(
                refused=False,
                retrieval_hit=False,
                status="fail",
                should_refuse=False,
                verdict=None,
                faithfulness=0.85,
            )
            == UNCLASSIFIED
        )

    def test_faithfulness_unknown_falls_through(self) -> None:
        """规则 4：faithfulness 缺失（如裁判未评）不硬猜 → unclassified。"""
        assert (
            attribution_candidate(
                refused=False,
                retrieval_hit=False,
                status="fail",
                should_refuse=False,
                verdict=None,
                faithfulness=None,
            )
            == UNCLASSIFIED
        )

    @pytest.mark.parametrize("verdict", ["off_topic", "wrong_number"])
    def test_hit_but_off_track_is_generation_suspect(self, verdict: str) -> None:
        """规则 5：资料在手却答偏 / 答错数字。"""
        assert (
            attribution_candidate(
                refused=False,
                retrieval_hit=True,
                status="fail",
                should_refuse=False,
                verdict=verdict,
                faithfulness=None,
            )
            == GENERATION_SUSPECT
        )

    def test_hit_with_other_verdict_unclassified(self) -> None:
        """规则 5 反例：verdict 不在 {off_topic, wrong_number} → unclassified。"""
        assert (
            attribution_candidate(
                refused=False,
                retrieval_hit=True,
                status="fail",
                should_refuse=False,
                verdict="correct",
                faithfulness=None,
            )
            == UNCLASSIFIED
        )

    def test_refused_without_hit_is_unclassified(self) -> None:
        """其余：拒了且没有命中信息（旧 Q029 型）→ 诚实不归类。"""
        assert (
            attribution_candidate(
                refused=True,
                retrieval_hit=None,
                status="fail",
                should_refuse=True,
                verdict=None,
                faithfulness=None,
            )
            == UNCLASSIFIED
        )


def _register_current_golden(pg_engine: Engine, *, name: str, checksum: str) -> int:
    """按当前 golden_set 真实版本注册数据集（checksum 由调用方决定真假）。"""
    with Session(pg_engine) as session:
        return register_dataset(
            session,
            name=name,
            version=loader.DATASET_VERSION,
            repo_path=str(GOLDEN_SET_PATH),
            file_path=None,
            file_format="yaml",
            checksum=checksum,
            item_count=31,
            distribution={"normal": 18, "refuse": 6, "permission": 4, "edge": 3},
            tag_stats={},
            unconfirmed_count=0,
        )


def _add_fail_item(
    pg_engine: Engine,
    run_id: int,
    *,
    item_id: str,
    refused: bool,
    retrieval_hit: bool | None = None,
) -> None:
    with Session(pg_engine) as session:
        insert_run_items(
            session,
            run_id,
            [
                EvalRunItem(
                    run_id=run_id,
                    item_id=item_id,
                    category="edge",
                    question="测试题",
                    status="fail",
                    answer="测试答案",
                    refused=refused,
                    retrieval_hit=retrieval_hit,
                    scores={"faithfulness": None},
                )
            ],
        )
        session.commit()


class TestAttributeRunIntegration:
    """真机 PG：checksum 门禁 + refuse 类回填 + R15 边界。

    只用 Q029（expected_sources=[]）构造 fail 条目——没有标准来源就不会
    触发检索重跑，本测试因此不依赖 Ollama / 嵌入服务。
    """

    def test_checksum_mismatch_refuses_to_start(self, pg: Engine) -> None:
        """R14：数据集 checksum 与当前 golden_set 不一致 → 拒绝开始。"""
        ds_id = _register_current_golden(
            pg, name=f"{NAME_PREFIX}-badsum", checksum="0" * 64
        )
        with Session(pg) as session:
            run_id = create_run(
                session,
                dataset_id=ds_id,
                git_commit="test000",
                config_snapshot={},
                model_config_ids=[1],
            )
        with Session(pg) as session, pytest.raises(SystemExit, match="checksum"):
            attribute_run(session, run_id)

    def test_refuse_item_backfill_and_r15_boundary(self, pg: Engine) -> None:
        """Q029 型（refused=true、无 expected_sources）：candidate 落库、
        retrieval_hit 保持 None、attribution_confirmed 仍为 NULL（R15）。"""
        real_checksum = loader.file_checksum(GOLDEN_SET_PATH)
        ds_id = _register_current_golden(
            pg, name=f"{NAME_PREFIX}-ok", checksum=real_checksum
        )
        with Session(pg) as session:
            run_id = create_run(
                session,
                dataset_id=ds_id,
                git_commit="test000",
                config_snapshot={},
                model_config_ids=[1],
            )
        _add_fail_item(pg, run_id, item_id="Q029", refused=True)
        # 题集里不存在的题：仍回填 unclassified，保证统计口径完整
        _add_fail_item(pg, run_id, item_id="Q999", refused=False, retrieval_hit=False)

        with Session(pg) as session:
            processed = attribute_run(session, run_id, kb_id=534)
        assert processed == 2

        with Session(pg) as session:
            rows = {
                r.item_id: r
                for r in list_run_items(session, run_id, status="fail")
            }
            assert rows["Q029"].attribution_candidate == UNCLASSIFIED
            assert rows["Q029"].retrieval_hit is None, (
                "无 expected_sources 的题不重跑检索，hit 保持 None"
            )
            assert rows["Q999"].attribution_candidate == UNCLASSIFIED
            for row in rows.values():
                assert row.attribution_confirmed is None, "R15：机器绝不写结论"
                assert row.confirmed_by is None
                assert row.confirmed_at is None

    def test_update_candidate_repo_function(self, pg: Engine) -> None:
        """repo 层：update_attribution_candidate 自带 commit 且只写候选半区。"""
        ds_id = _register_current_golden(
            pg, name=f"{NAME_PREFIX}-repo", checksum="0" * 64
        )
        with Session(pg) as session:
            run_id = create_run(
                session,
                dataset_id=ds_id,
                git_commit="test000",
                config_snapshot={},
                model_config_ids=[1],
            )
        _add_fail_item(pg, run_id, item_id="Q001", refused=True, retrieval_hit=None)

        with Session(pg) as session:
            update_attribution_candidate(
                session, run_id, "Q001",
                retrieval_hit=True, candidate=GENERATION_SUSPECT,
            )

        # 新会话读回（验证真的 commit 了）
        with Session(pg) as session:
            row = list_run_items(session, run_id, status="fail")[0]
            assert row.retrieval_hit is True
            assert row.attribution_candidate == GENERATION_SUSPECT
            assert row.attribution_confirmed is None
