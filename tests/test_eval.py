"""T28 评估脚本：loader 校验 / 指标纯函数 / 数据集注册 / runner 编排。

分层：
- TestLoader：纯文件测试（tmp_path），不依赖任何外部服务；
- TestMetrics：纯函数已知输入 → 已知输出；
- TestEnsureDataset + TestRunner：真机 PostgreSQL 集成（与 test_eval_repo.py
  同风格：name 前缀隔离、前后各清一次），**不依赖 Ollama**——runner 编排
  注入假链路（chain 参数与 monkeypatch 的 resolve_permission）。
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

from app.eval import runner
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
        outcomes = [
            # normal 题：有标准来源、未拒答 → pass
            ItemOutcome(
                item=_item("Q001", expected_sources=["a.txt"]),
                verdict="pass",
                scores={"recall": 1.0, "rr": 1.0, "precision": 0.6,
                        "prompt_tokens": 10, "completion_tokens": 5},
                retrieval_hit=True, refused=False, latency_ms=1000,
            ),
            # refuse 题：拒答 → pass
            ItemOutcome(
                item=_item("Q022", category="refuse", should_refuse=True),
                verdict="pass",
                scores={"recall": None, "rr": None, "precision": None,
                        "prompt_tokens": 0, "completion_tokens": 0},
                refused=True, latency_ms=2000,
            ),
            # edge 题（Q028 形态）：无标准来源、无拒答、不计延迟
            ItemOutcome(
                item=_item("Q028", category="edge"),
                verdict="pass",
                scores={"recall": None, "rr": None, "precision": None,
                        "prompt_tokens": 0, "completion_tokens": 0},
                refused=None, count_in_latency=False,
            ),
        ]
        m = aggregate(outcomes)
        assert m["layer1"]["recall_at_5"] == 1.0
        assert m["layer1"]["sample_size"] == 1, "layer1 只统计有标准来源的题"
        assert m["layer3"]["refusal_accuracy"] == 1.0
        assert m["layer3"]["false_refusal_rate"] == 0.0
        assert m["layer3"]["task_completion"] == 1.0
        assert m["latency_eval"]["sample_size"] == 2, "Q028 不计入延迟"
        assert m["latency_eval"]["p95_ms"] == 2000
        assert m["tokens"] == {"prompt": 10, "completion": 5}
        assert m["items"] == {"total": 3, "passed": 3, "failed": 0}


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

        verdicts = {o.item.item_id: o.verdict for o in outcomes}
        assert verdicts == {
            "Q001": "pass",   # 正常作答 + 检索命中
            "Q002": "pass",   # 正常作答（检索 miss 不影响行为层 verdict）
            "Q022": "pass",   # 应拒答且拒答
            "Q028": "pass",   # 超长被 schema 拦截
        }
        by_id = {o.item.item_id: o for o in outcomes}
        assert by_id["Q001"].retrieval_hit is True
        assert by_id["Q002"].retrieval_hit is False
        assert by_id["Q028"].count_in_latency is False
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
            assert q001.answer == "满 1 年 5 天 [1]", "answer 全文必须落库"
            assert q001.retrieved_chunk_ids == [101, 102]
            assert q001.scores["recall"] == 1.0  # type: ignore[index]
            q028 = next(r for r in rows if r.item_id == "Q028")
            assert "schema 校验拦截" in (q028.notes or "")

    def test_refused_normal_item_is_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """误拒判定：normal 题被拒答 → verdict=fail 且计入误拒率。"""
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
        assert outcomes[0].verdict == "fail"
        assert "误拒" in outcomes[0].notes
        assert aggregate(outcomes)["layer3"]["false_refusal_rate"] == 1.0
