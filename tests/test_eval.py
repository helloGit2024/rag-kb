"""T28 评估脚本 + T29 裁判评分：loader 校验 / 指标纯函数 / 数据集注册 /
runner 编排 / 裁判解析与 verdict 推导。

分层：
- TestLoader：纯文件测试（tmp_path），不依赖任何外部服务；
- TestMetrics / TestJudgeParse / TestDeriveVerdict：纯函数已知输入 → 已知输出；
- TestJudgeAnswer：httpx.MockTransport 假裁判 HTTP，不依赖 Ollama；
- TestEnsureDataset + TestRunner：真机 PostgreSQL 集成（与 test_eval_repo.py
  同风格：name 前缀隔离、前后各清一次），**不依赖 Ollama**——runner 编排
  注入假链路（chain 参数与 monkeypatch 的 resolve_permission）与假裁判。
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.eval import runner
from app.eval.judge import (
    JudgeOutcome,
    JudgeScores,
    derive_verdict,
    judge_answer,
    parse_judge_json,
)
from app.eval.loader import (
    GoldenItem,
    GoldenSetError,
    ensure_dataset,
    load_golden_set,
)
from app.eval.runner import ItemOutcome, aggregate, layer1_scores, percentile
from app.repos import eval_repo
from app.repos.chunk_repo import ChunkHit
from app.repos.models import EvalDataset, EvalRun, EvalRunItem
from app.services.generator import REFUSAL_TEXT
from app.services.permission import PermissionBundle
from app.services.qa import QaResult

NAME_PREFIX = "test-eval-t28"


# ── 工具 ────────────────────────────────────────────────────────────────────


def _write_yaml(path: Path, entries: list[dict]) -> Path:
    """最小 YAML 序列化：测试条目字段简单，手工拼装即可（避免再引依赖）。"""
    lines: list[str] = []
    for e in entries:
        lines.append(f"- id: {e['id']}")
        for key, value in e.items():
            if key == "id":
                continue
            if isinstance(value, list):
                lines.append(f"  {key}:")
                lines += [f'    - "{v}"' for v in value] or ["    []"]
            elif isinstance(value, bool):
                lines.append(f"  {key}: {'true' if value else 'false'}")
            else:
                lines.append(f'  {key}: "{value}"')
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _entry(item_id: str, **overrides) -> dict:
    base = {
        "id": item_id,
        "version": "V0.1",
        "question": "测试问题",
        "category": "normal",
        "expected_answer": "标准答案",
        "should_refuse": False,
        "scope_keys": "public",
    }
    base.update(overrides)
    return base


def _item(item_id: str, **overrides) -> GoldenItem:
    base = {
        "item_id": item_id,
        "category": "normal",
        "question": "测试问题",
        "expected_answer": "标准答案",
    }
    base.update(overrides)
    return GoldenItem(**base)


def _pg_available() -> bool:
    if not os.environ.get("DATABASE_URL"):
        return False
    try:
        with socket.create_connection(("localhost", 5432), timeout=1):
            pass
    except OSError:
        return False
    return True


_NEEDS_PG = pytest.mark.skipif(
    not _pg_available(), reason="PostgreSQL 未启动，跳过 T28 集成测试"
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


# ── loader：加载与校验（纯文件）──────────────────────────────────────────────


class TestLoader:
    def test_filters_version(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "gs.yaml",
            [_entry("Q001"), _entry("Q031", version="V1.0"), _entry("Q002")],
        )
        items = load_golden_set(path, version="V0.1")
        assert [i.item_id for i in items] == ["Q001", "Q002"], (
            "只取 V0.1 且按 id 排序"
        )

    def test_rejects_duplicate_id(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "gs.yaml", [_entry("Q001"), _entry("Q001")])
        with pytest.raises(GoldenSetError, match="重复"):
            load_golden_set(path, version="V0.1")

    def test_rejects_bad_category(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "gs.yaml", [_entry("Q001", category="weird")])
        with pytest.raises(GoldenSetError, match="category"):
            load_golden_set(path, version="V0.1")

    def test_rejects_refuse_without_should_refuse(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "gs.yaml", [_entry("Q001", category="refuse")]
        )
        with pytest.raises(GoldenSetError, match="should_refuse"):
            load_golden_set(path, version="V0.1")

    def test_rejects_unknown_scope_keys(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path / "gs.yaml", [_entry("Q001", scope_keys="dept:不存在的部")]
        )
        with pytest.raises(GoldenSetError, match="身份映射"):
            load_golden_set(path, version="V0.1")

    def test_rejects_empty_version(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "gs.yaml", [_entry("Q001", version="V9.9")])
        with pytest.raises(GoldenSetError, match="V0.1"):
            load_golden_set(path, version="V0.1")

    def test_expected_doc_titles_strips_anchor(self) -> None:
        item = _item(
            "Q001", expected_sources=["员工手册.txt#年假章节", "其他.txt"]
        )
        assert item.expected_doc_titles == ["员工手册.txt", "其他.txt"]

    def test_identity_mapping(self) -> None:
        assert _item("Q1", scope_keys="dept:研发部").username == "erin"
        assert _item("Q2", scope_keys="dept:销售部").username == "alice"
        assert _item("Q3", scope_keys="public").username == "erin"

    def test_judge_field_default_and_human(self, tmp_path: Path) -> None:
        """judge 缺省 = model；显式 human 被解析（T29）。"""
        path = _write_yaml(
            tmp_path / "gs.yaml", [_entry("Q001"), _entry("Q002", judge="human")]
        )
        items = {i.item_id: i for i in load_golden_set(path, version="V0.1")}
        assert items["Q001"].judge == "model"
        assert items["Q002"].judge == "human"

    def test_rejects_bad_judge_value(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "gs.yaml", [_entry("Q001", judge="robot")])
        with pytest.raises(GoldenSetError, match="judge"):
            load_golden_set(path, version="V0.1")


# ── 指标纯函数 ───────────────────────────────────────────────────────────────


class TestMetrics:
    def test_layer1_hit_at_first(self) -> None:
        recall, rr, precision = layer1_scores(
            ["员工手册.txt", "其他.txt"], ["员工手册.txt"]
        )
        assert (recall, rr, precision) == (1.0, 1.0, 0.5)

    def test_layer1_hit_at_third(self) -> None:
        recall, rr, precision = layer1_scores(
            ["a.txt", "b.txt", "员工手册.txt", "员工手册.txt", "c.txt"],
            ["员工手册.txt"],
        )
        assert recall == 1.0
        assert rr == pytest.approx(1 / 3)
        assert precision == pytest.approx(2 / 5)

    def test_layer1_miss(self) -> None:
        recall, rr, precision = layer1_scores(["a.txt", "b.txt"], ["员工手册.txt"])
        assert (recall, rr, precision) == (0.0, 0.0, 0.0)

    def test_percentile(self) -> None:
        assert percentile([], 95) == 0.0
        assert percentile([100, 200, 300, 400, 500], 95) == 500
        assert percentile([100, 200, 300, 400, 500], 50) == 300

    def test_aggregate_denominators(self) -> None:
        """分母口径：layer1 只算有标准来源的题；拒答/误拒按 should_refuse 分。"""
        scored = runner._zero_scores()
        scored.update({"recall": 1.0, "rr": 1.0, "precision": 0.6,
                       "prompt_tokens": 10, "completion_tokens": 5})
        outcomes = [
            # normal 题：有标准来源、未拒答 → pass
            ItemOutcome(
                item=_item("Q001", expected_sources=["a.txt"]),
                status="pass",
                scores=scored,
                retrieval_hit=True, refused=False, latency_ms=1000,
            ),
            # refuse 题：拒答 → pass
            ItemOutcome(
                item=_item("Q022", category="refuse", should_refuse=True),
                status="pass",
                scores=runner._zero_scores(),
                refused=True, latency_ms=2000,
            ),
            # edge 题（Q028 形态）：无标准来源、无拒答、不计延迟
            ItemOutcome(
                item=_item("Q028", category="edge"),
                status="pass",
                scores=runner._zero_scores(),
                refused=None, count_in_latency=False,
            ),
        ]
        m = aggregate(outcomes)
        assert m["layer1"]["recall_at_5"] == 1.0
        assert m["layer1"]["sample_size"] == 1, "layer1 只统计有标准来源的题"
        assert m["layer2"]["sample_size"] == 0, "三题都未跑裁判，layer2 为空"
        assert m["layer2"]["faithfulness"] is None
        assert m["layer3"]["refusal_accuracy"] == 1.0
        assert m["layer3"]["false_refusal_rate"] == 0.0
        assert m["layer3"]["task_completion"] == 1.0
        assert m["latency_eval"]["sample_size"] == 2, "Q028 不计入延迟"
        assert m["latency_eval"]["p95_ms"] == 2000
        assert m["tokens"] == {
            "prompt": 10, "completion": 5,
            "judge_prompt": 0, "judge_completion": 0,
        }
        assert m["items"] == {"total": 3, "passed": 3, "failed": 0}

    def test_aggregate_layer2_means(self) -> None:
        """第 2 层：均值分母 = 裁判成功的题；numeric_accuracy 分母再缩到非 null。"""
        def _judged(fid: str, faith: float, rel: float, cit: float,
                    num: float | None) -> ItemOutcome:
            scores = runner._zero_scores()
            scores.update({
                "faithfulness": faith, "answer_relevance": rel,
                "citation_accuracy": cit, "completeness": 1.0,
                "numeric_accuracy": num, "judge_prompt_tokens": 50,
                "judge_completion_tokens": 10,
            })
            return ItemOutcome(
                item=_item(fid), status="pass", verdict="correct",
                scores=scores, refused=False,
            )

        outcomes = [
            _judged("Q001", 1.0, 1.0, 1.0, 1.0),
            _judged("Q002", 0.5, 0.5, 0.0, None),
            # 裁判失败的题：维度全 null，不进 layer2 分母
            ItemOutcome(
                item=_item("Q003"), status="pass", verdict=None,
                scores=runner._zero_scores(), refused=False,
                notes="裁判失败（2 次调用均未产出合法结果）",
            ),
        ]
        m = aggregate(outcomes)["layer2"]
        assert m["sample_size"] == 2, "分母只含裁判成功的题（诚实口径）"
        assert m["faithfulness"] == 0.75
        assert m["answer_relevance"] == 0.75
        assert m["citation_accuracy"] == 0.5
        assert m["numeric_accuracy"] == 1.0, "null 不进 numeric 分母"


# ── ensure_dataset：注册 / 复用 / checksum 门禁（真机 PG）────────────────────


@_NEEDS_PG
class TestEnsureDataset:
    def test_register_reuse_and_checksum_gate(
        self, pg: Engine, tmp_path: Path
    ) -> None:
        path = _write_yaml(tmp_path / "gs.yaml", [_entry("Q001")])
        items = load_golden_set(path, version="V0.1")
        name = f"{NAME_PREFIX}-ds"

        with Session(pg) as session:
            ds_id = ensure_dataset(session, path=path, items=items, name=name)
            row = session.get(EvalDataset, ds_id)
            assert row is not None
            assert row.state == "active", "unconfirmed=0 必须能激活"
            assert row.item_count == 1
            assert row.distribution == {"normal": 1}

            again = ensure_dataset(session, path=path, items=items, name=name)
            assert again == ds_id, "同一份文件必须复用同一个数据集"

        # 文件内容被改（加一题）→ checksum 变化 → 必须拒绝开始（R14）
        _write_yaml(path, [_entry("Q001"), _entry("Q002")])
        items2 = load_golden_set(path, version="V0.1")
        with Session(pg) as session, pytest.raises(GoldenSetError, match="checksum"):
            ensure_dataset(session, path=path, items=items2, name=name)


# ── runner 编排：假链路 + 真机 PG ────────────────────────────────────────────


def _fake_chain_factory(refused_ids: set[str]):
    """造一条假链路：question 里含题号（测试问题即题号），在 refused_ids 里的
    返回固定拒答句，否则返回带引用的正常答案；hits 恒为两份文档。"""

    def _chain(
        question: str, *, top_k: int, scope_keys: list[str], kb_ids: list[int]
    ) -> tuple[QaResult, list[ChunkHit]]:
        assert kb_ids == [999], "kb 轴必须锁定评估库"
        hits = [
            ChunkHit(
                chunk_id=101, content="块内容一", doc_id=1, doc_title="员工手册.txt",
                char_start=0, char_end=10, score=0.91,
            ),
            ChunkHit(
                chunk_id=102, content="块内容二", doc_id=2, doc_title="其他.txt",
                char_start=0, char_end=10, score=0.80,
            ),
        ]
        if question in refused_ids:
            return (
                QaResult(answer=REFUSAL_TEXT, refused=True, citations=[],
                         retrieve_ms=10, generate_ms=20, latency_ms=30),
                hits,
            )
        return (
            QaResult(answer="满 1 年 5 天 [1]", refused=False, citations=[],
                     retrieve_ms=10, generate_ms=20, latency_ms=30,
                     prompt_tokens=100, completion_tokens=20),
            hits,
        )

    return _chain


@_NEEDS_PG
class TestRunner:
    def test_end_to_end_with_fake_chain(
        self, pg: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 run(done) + N items 落库；verdict / retrieval_hit / answer 全文正确。"""
        monkeypatch.setattr(
            runner,
            "resolve_permission",
            lambda session, *, username: PermissionBundle(scope_keys=["public"]),
        )
        items = [
            # question 直接用题号：假链路按它决定拒答与否
            GoldenItem("Q001", "normal", "Q001", "x",
                       expected_sources=["员工手册.txt#年假"]),
            GoldenItem("Q002", "normal", "Q002", "x",
                       expected_sources=["不存在的文档.txt"]),  # 检索必 miss
            GoldenItem("Q022", "refuse", "Q022", "x", should_refuse=True),
            GoldenItem("Q028", "edge", "Q028", "x"),  # 超长题走 schema 校验路径
        ]
        path = _write_yaml(
            tmp_path / "gs.yaml",
            [
                _entry("Q001", expected_sources=["员工手册.txt#年假"]),
                _entry("Q002", expected_sources=["不存在的文档.txt"]),
                _entry("Q022", category="refuse", should_refuse=True),
                _entry("Q028", category="edge"),
            ],
        )
        # 假链路：Q022 拒答（正确），其余正常作答
        chain = _fake_chain_factory(refused_ids={"Q022"})

        with Session(pg) as session:
            ds_id = ensure_dataset(
                session, path=path, items=load_golden_set(path, version="V0.1"),
                name=f"{NAME_PREFIX}-run",
            )
            run_id = eval_repo.create_run(
                session, dataset_id=ds_id, git_commit="t28test",
                config_snapshot={"kb": 999}, model_config_ids=None,
            )
            outcomes = runner.run_all_items(session, items, kb_id=999, chain=chain)
            metrics = aggregate(outcomes)
            eval_repo.insert_run_items(
                session, run_id, [o.to_row(run_id) for o in outcomes]
            )
            eval_repo.finish_run(
                session, run_id, status="done", metrics=metrics,
                total_tokens_in=metrics["tokens"]["prompt"],
                total_tokens_out=metrics["tokens"]["completion"],
            )

        statuses = {o.item.item_id: o.status for o in outcomes}
        assert statuses == {
            "Q001": "pass",   # 正常作答 + 检索命中
            "Q002": "pass",   # 正常作答（检索 miss 不影响行为层 status）
            "Q022": "pass",   # 应拒答且拒答
            "Q028": "pass",   # 超长被 schema 拦截
        }
        by_id = {o.item.item_id: o for o in outcomes}
        assert by_id["Q001"].retrieval_hit is True
        assert by_id["Q002"].retrieval_hit is False
        assert by_id["Q028"].count_in_latency is False
        assert all(o.verdict is None for o in outcomes), "未注入裁判 → verdict 全 null"
        assert metrics["tokens"]["prompt"] == 200, "Q001/Q002 各 100，拒答与 Q028 为 0"

        with Session(pg) as session:
            run = eval_repo.get_run(session, run_id)
            assert run.status == "done"
            assert run.metrics["layer1"]["sample_size"] == 2  # type: ignore[index]
            rows = session.scalars(
                select(EvalRunItem).where(EvalRunItem.run_id == run_id)
            ).all()
            assert len(rows) == 4
            q001 = next(r for r in rows if r.item_id == "Q001")
            assert q001.status == "pass" and q001.verdict is None
            assert q001.answer == "满 1 年 5 天 [1]", "answer 全文必须落库"
            assert q001.retrieved_chunk_ids == [101, 102]
            assert q001.scores["recall"] == 1.0  # type: ignore[index]
            q028 = next(r for r in rows if r.item_id == "Q028")
            assert "schema 校验拦截" in (q028.notes or "")

    def test_refused_normal_item_is_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """误拒判定：normal 题被拒答 → status=fail 且计入误拒率。"""
        monkeypatch.setattr(
            runner,
            "resolve_permission",
            lambda session, *, username: PermissionBundle(scope_keys=["public"]),
        )
        items = [
            GoldenItem("Q001", "normal", "Q001", "x",
                       expected_sources=["员工手册.txt"])
        ]
        chain = _fake_chain_factory(refused_ids={"Q001"})
        outcomes = runner.run_all_items(None, items, kb_id=999, chain=chain)  # type: ignore[arg-type]
        assert outcomes[0].status == "fail"
        assert "误拒" in outcomes[0].notes
        assert aggregate(outcomes)["layer3"]["false_refusal_rate"] == 1.0

    def test_judge_skip_rules_and_scores(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T29 裁判触发规则：仅 normal/edge 且 judge=model 的题调裁判；
        裁判分数进 scores，verdict 取裁判推导值，layer2 分母 = 裁判成功题数。
        """
        monkeypatch.setattr(
            runner,
            "resolve_permission",
            lambda session, *, username: PermissionBundle(scope_keys=["public"]),
        )
        items = [
            GoldenItem("Q001", "normal", "Q001", "x",
                       expected_sources=["员工手册.txt"]),          # 应调裁判
            GoldenItem("Q008", "normal", "Q008", "x", judge="human",
                       expected_sources=["员工手册.txt"]),          # human → 跳过
            GoldenItem("Q022", "refuse", "Q022", "x",
                       should_refuse=True),                          # refuse → 跳过
            GoldenItem("Q030", "edge", "Q030", "x", judge="human",
                       expected_sources=["员工手册.txt"]),          # human → 跳过
            GoldenItem("Q029", "edge", "Q029", "x"),                 # 应调裁判
        ]
        chain = _fake_chain_factory(refused_ids={"Q022"})
        called: list[str] = []

        def _fake_judge(
            item: GoldenItem, result: QaResult, hits: list[ChunkHit]
        ) -> JudgeOutcome:
            called.append(item.item_id)
            return JudgeOutcome(
                scores=JudgeScores(
                    faithfulness=1.0, answer_relevancy=1.0, completeness=1.0,
                    numeric_accuracy=None, citation_accuracy=0.5,
                    reasoning="逐维度理由", critical_issue=None,
                ),
                verdict="correct",
                prompt_tokens=50, completion_tokens=10,
            )

        outcomes = runner.run_all_items(
            None, items, kb_id=999, chain=chain, judge=_fake_judge  # type: ignore[arg-type]
        )
        assert called == ["Q001", "Q029"], "human/refuse 题必须跳过裁判"
        by_id = {o.item.item_id: o for o in outcomes}
        q001 = by_id["Q001"]
        assert q001.verdict == "correct"
        assert q001.scores["faithfulness"] == 1.0
        assert q001.scores["citation_accuracy"] == 0.5
        assert q001.scores["judge_reasoning"] == "逐维度理由"
        assert q001.scores["judge_prompt_tokens"] == 50
        assert by_id["Q008"].verdict is None, "human 题等人工评分"
        assert by_id["Q008"].scores["faithfulness"] is None
        assert by_id["Q022"].verdict is None, "refuse 题用程序断言，不走裁判"

        m = aggregate(outcomes)
        assert m["layer2"]["sample_size"] == 2
        assert m["layer2"]["faithfulness"] == 1.0
        assert m["layer2"]["citation_accuracy"] == 0.5
        assert m["tokens"]["judge_prompt"] == 100

    def test_judge_failure_does_not_break_item(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """裁判失败：题的行为判定不受影响，维度置 null、错误写 notes（不中断）。"""
        monkeypatch.setattr(
            runner,
            "resolve_permission",
            lambda session, *, username: PermissionBundle(scope_keys=["public"]),
        )
        items = [GoldenItem("Q001", "normal", "Q001", "x",
                            expected_sources=["员工手册.txt"])]
        chain = _fake_chain_factory(refused_ids=set())

        def _broken_judge(
            item: GoldenItem, result: QaResult, hits: list[ChunkHit]
        ) -> JudgeOutcome:
            return JudgeOutcome(
                scores=None, verdict=None,
                error="裁判失败（2 次调用均未产出合法结果）：输出不含 JSON",
            )

        outcomes = runner.run_all_items(
            None, items, kb_id=999, chain=chain, judge=_broken_judge  # type: ignore[arg-type]
        )
        o = outcomes[0]
        assert o.status == "pass", "裁判挂 ≠ 题挂（行为层独立判定）"
        assert o.verdict is None
        assert o.scores["faithfulness"] is None
        assert "裁判失败" in o.notes
        assert aggregate(outcomes)["layer2"]["sample_size"] == 0


# ── 裁判：JSON 解析 / verdict 推导 / 调用（纯函数 + MockTransport）────────────

_VALID_JUDGE_JSON = (
    '{"faithfulness": 1.0, "answer_relevancy": 0.5, "completeness": 1.0,'
    ' "numeric_accuracy": null, "citation_accuracy": 1.0,'
    ' "reasoning": "各维度理由", "critical_issue": null}'
)


def _scores(**overrides) -> JudgeScores:
    base = {
        "faithfulness": 1.0, "answer_relevancy": 1.0, "completeness": 1.0,
        "numeric_accuracy": None, "citation_accuracy": 1.0,
        "reasoning": "r", "critical_issue": None,
    }
    base.update(overrides)
    return JudgeScores(**base)


class TestJudgeParse:
    def test_clean_json(self) -> None:
        s = parse_judge_json(_VALID_JUDGE_JSON)
        assert s.faithfulness == 1.0
        assert s.answer_relevancy == 0.5
        assert s.numeric_accuracy is None, "无数字题合法为 null"
        assert s.citation_accuracy == 1.0
        assert s.critical_issue is None

    def test_fenced_and_noisy_json(self) -> None:
        """容忍 ```json 围栏与前后絮叨（真实模型常犯）。"""
        noisy = f"好的，我来评分：\n```json\n{_VALID_JUDGE_JSON}\n```\n以上。"
        s = parse_judge_json(noisy)
        assert s.faithfulness == 1.0

    def test_rejects_missing_field(self) -> None:
        with pytest.raises(ValueError, match="citation_accuracy"):
            parse_judge_json(
                '{"faithfulness": 1.0, "answer_relevancy": 1.0,'
                ' "completeness": 1.0, "numeric_accuracy": null,'
                ' "reasoning": "r", "critical_issue": null}'
            )

    def test_rejects_out_of_range(self) -> None:
        with pytest.raises(ValueError, match="越界"):
            parse_judge_json(_VALID_JUDGE_JSON.replace('"faithfulness": 1.0',
                                                       '"faithfulness": 1.5'))

    def test_rejects_bad_critical_issue(self) -> None:
        with pytest.raises(ValueError, match="critical_issue"):
            parse_judge_json(_VALID_JUDGE_JSON.replace(
                '"critical_issue": null', '"critical_issue": "weird"'))

    def test_rejects_non_json(self) -> None:
        with pytest.raises(ValueError, match="JSON"):
            parse_judge_json("我觉得这个答案整体不错，给 8 分。")


class TestDeriveVerdict:
    def test_critical_issue_priority(self) -> None:
        assert derive_verdict(_scores(
            critical_issue="must_refuse_but_answered")) == "under_refusal"
        assert derive_verdict(_scores(
            critical_issue="false_refuse")) == "over_refusal"
        # 泄露类不在 verdict 枚举内 → null（进人工复核，不硬塞别的桶）
        assert derive_verdict(_scores(
            critical_issue="leaked_restricted_content")) is None

    def test_score_thresholds(self) -> None:
        assert derive_verdict(_scores(faithfulness=0.5)) == "hallucination"
        assert derive_verdict(_scores(numeric_accuracy=0.0)) == "wrong_number"
        assert derive_verdict(_scores(answer_relevancy=0.5)) == "off_topic"
        assert derive_verdict(_scores()) == "correct"

    def test_faithfulness_beats_numeric(self) -> None:
        """优先级：忠实度问题先于数字问题（幻觉比抄错数更根本）。"""
        assert derive_verdict(_scores(
            faithfulness=0.0, numeric_accuracy=0.0)) == "hallucination"


class TestJudgeAnswer:
    """HTTP 层：MockTransport 假裁判（不依赖 Ollama）。"""

    def _hits(self) -> list[ChunkHit]:
        return [
            ChunkHit(chunk_id=101, content="满 1 年 5 天年假", doc_id=1,
                     doc_title="员工手册.txt", char_start=0, char_end=10,
                     score=0.91),
        ]

    def _transport(self, contents: list[str]) -> httpx.MockTransport:
        it = iter(contents)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant",
                                         "content": next(it)}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10,
                          "total_tokens": 60},
                "model": "judge-fake",
            })

        return httpx.MockTransport(handler)

    def test_success_first_try(self) -> None:
        perfect = _VALID_JUDGE_JSON.replace('"answer_relevancy": 0.5',
                                            '"answer_relevancy": 1.0')
        out = judge_answer(
            question="年假几天", expected_answer="5 天",
            hits=self._hits(), actual_answer="满 1 年 5 天 [1]",
            model="judge-fake", template="Q{{question}}",
            transport=self._transport([perfect]),
        )
        assert out.scores is not None and out.scores.faithfulness == 1.0
        assert out.verdict == "correct"
        assert (out.prompt_tokens, out.completion_tokens) == (50, 10)
        assert not out.error

    def test_retry_once_on_garbage(self) -> None:
        """第一次输出非 JSON → 补问 1 次成功；Token 两次都记账。"""
        out = judge_answer(
            question="q", expected_answer="e", hits=self._hits(),
            actual_answer="a", model="judge-fake", template="t",
            transport=self._transport(["先聊聊……", _VALID_JUDGE_JSON]),
        )
        assert out.scores is not None
        assert (out.prompt_tokens, out.completion_tokens) == (100, 20)

    def test_gives_up_after_two_failures(self) -> None:
        """两次都产不出合法 JSON → scores=None + error，不抛异常（不中断 run）。"""
        out = judge_answer(
            question="q", expected_answer="e", hits=self._hits(),
            actual_answer="a", model="judge-fake", template="t",
            transport=self._transport(["废话一", "废话二"]),
        )
        assert out.scores is None and out.verdict is None
        assert "裁判失败" in out.error
        assert (out.prompt_tokens, out.completion_tokens) == (100, 20)
