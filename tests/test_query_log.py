"""T34 query_logs：问答流水落表的契约测试。

三类断言：
  1. **旁路语义**（纯单测）：落表失败不得影响问答返回——用户看到的仍是答案，
     不是 500；失败必须显式打到 stderr（R7：可见，但不阻断服务）。
  2. **落表内容**（纯单测 + monkeypatch）：kb_id 单库/多库、scope_snapshot 两轴、
     引用序列化、成本透传——用假 repo 捕获参数，不连数据库。
  3. **真库往返**（需 PostgreSQL）：insert_log 写入后能按 id 读回，
     确认 DDL 与 ORM 一致（迁移与模型不漂移）。

不依赖真 Redis；纯单测直接调 `qa._log_qa`，回避嵌入/检索/生成。
"""

from __future__ import annotations

import os
import socket
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.repos import query_log_repo
from app.repos.models import QueryLog
from app.services import qa
from app.services.generator import Citation

_Q = "T34 落表用的测试问题"


def _make_result(**over: Any) -> qa.QaResult:
    """构造一条完整 QaResult（默认非拒答、有一个引用）。"""
    base: dict[str, Any] = {
        "answer": "答",
        "refused": False,
        "citations": [
            Citation(
                index=1,
                doc_id=1,
                doc_title="制度",
                chunk_id=9,
                char_start=0,
                char_end=2,
                snippet="片段",
                score=0.9,
            )
        ],
        "retrieve_ms": 10,
        "generate_ms": 20,
        "latency_ms": 30,
        "model_used": "qwen2.5:7b",
        "cost_cny": 0.0123,
    }
    base.update(over)
    return qa.QaResult(**base)


class _Capture:
    """假 insert_log：记录参数并返回固定 id。"""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None
        self.calls = 0

    def __call__(self, **kwargs: Any) -> int:
        self.calls += 1
        self.kwargs = kwargs
        return 42


# ── 落表内容（纯单测）──────────────────────────────────────────────


def test_single_kb_writes_kb_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """恰好一个知识库 → kb_id 列有值，scope_snapshot 两轴都记。"""
    cap = _Capture()
    monkeypatch.setattr(query_log_repo, "insert_log", cap)
    result = _make_result()

    qa._log_qa(
        _Q, ["internal", "dept:销售部"], result, [9, 11],
        cache_status="miss", user_id=7, kb_ids=[3],
    )

    assert cap.kwargs is not None
    assert cap.kwargs["kb_id"] == 3
    assert cap.kwargs["scope_snapshot"] == {
        "scope_keys": ["internal", "dept:销售部"],
        "kb_ids": [3],
    }
    assert cap.kwargs["retrieved_chunk_ids"] == [9, 11]
    # 引用必须是可 JSON 序列化的 dict（JSONB 列），不是 dataclass
    assert cap.kwargs["citations"] == [
        {
            "index": 1,
            "doc_id": 1,
            "doc_title": "制度",
            "chunk_id": 9,
            "char_start": 0,
            "char_end": 2,
            "snippet": "片段",
            "score": 0.9,
        }
    ]
    assert cap.kwargs["cost_cny"] == 0.0123
    assert result.query_log_id == 42


def test_multi_kb_leaves_kb_id_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """多个知识库 → kb_id 留 NULL，完整列表进 scope_snapshot（不丢信息）。"""
    cap = _Capture()
    monkeypatch.setattr(query_log_repo, "insert_log", cap)

    qa._log_qa(
        _Q, ["internal"], _make_result(), [],
        cache_status="hit", user_id=7, kb_ids=[3, 4],
    )

    assert cap.kwargs is not None
    assert cap.kwargs["kb_id"] is None
    assert cap.kwargs["scope_snapshot"]["kb_ids"] == [3, 4]


def test_no_user_id_skips_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """无身份（脚本/测试）→ 不落表，只打 stderr；query_log_id 保持 None。"""
    cap = _Capture()
    monkeypatch.setattr(query_log_repo, "insert_log", cap)
    result = _make_result()

    qa._log_qa(
        _Q, ["internal"], result, [], cache_status="miss", kb_ids=[3],
    )

    assert cap.calls == 0
    assert result.query_log_id is None


def test_none_cost_stays_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存命中回放 / 本地无单价表 → cost_cny=None 原样传（不伪装成 0）。"""
    cap = _Capture()
    monkeypatch.setattr(query_log_repo, "insert_log", cap)

    qa._log_qa(
        _Q, ["internal"],
        _make_result(cost_cny=None, model_used=None, retrieve_ms=0, generate_ms=0),
        [], cache_status="hit", user_id=7, kb_ids=[3],
    )

    assert cap.kwargs is not None
    assert cap.kwargs["cost_cny"] is None
    assert cap.kwargs["model_used"] is None


def test_db_failure_does_not_break_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """落表抛异常 → _log_qa 不向上抛；错误必须打到 stderr（R7 可见但不阻断）。"""
    def boom(**kwargs: Any) -> int:
        raise RuntimeError("PG 挂了")

    monkeypatch.setattr(query_log_repo, "insert_log", boom)
    result = _make_result()

    qa._log_qa(
        _Q, ["internal"], result, [], cache_status="miss", user_id=7, kb_ids=[3],
    )

    assert result.query_log_id is None
    err = capsys.readouterr().err
    assert "query_logs 落表失败" in err


# ── 真库往返（需 PostgreSQL）────────────────────────────────────────


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
    not _pg_available(), reason="PostgreSQL 未启动，跳过 T34 集成测试"
)


def _purge(engine: Engine) -> None:
    with Session(engine) as session:
        session.execute(delete(QueryLog).where(QueryLog.question == _Q))
        session.commit()


@_NEEDS_PG
def test_insert_and_read_back() -> None:
    """insert_log 写入后可读回：DDL 与 ORM 一致，两轴快照、数组、成本都落对。"""
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    _purge(engine)
    try:
        qa._log_qa(
            _Q, ["internal"], _make_result(), [9, 11],
            cache_status="miss", user_id=7, kb_ids=[3, 4],
        )
        row_id = _read_back(engine)
        assert row_id is not None
    finally:
        _purge(engine)


def _read_back(engine: Engine) -> int | None:
    with Session(engine) as session:
        row = session.scalars(
            select(QueryLog).where(QueryLog.question == _Q)
        ).one()
        assert row.user_id == 7
        assert row.kb_id is None  # 多库 → NULL
        assert row.scope_snapshot == {
            "scope_keys": ["internal"],
            "kb_ids": [3, 4],
        }
        assert row.retrieved_chunk_ids == [9, 11]
        assert row.refused is False
        assert row.citations is not None and len(row.citations) == 1
        assert float(row.cost_cny) == pytest.approx(0.0123, abs=0.0001)
        assert row.model_used == "qwen2.5:7b"
        # V0.1 恒 NULL 的两列
        assert row.rerank_ms is None
        assert row.trace_id is None
        assert row.created_at is not None
        return row.id
