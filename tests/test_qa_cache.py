"""T16：问答缓存 —— 权限隔离 + 可用性四条路径。

**两类断言合起来才算 T16 验收**：

- **隔离**（上半）：同一个问题、不同的可见范围 → 不同的缓存 key（R3）。
  若这条不成立，A 部门的问题会命中 B 部门的答案——越权泄露且极难复现。
- **可用性**（下半）：`answer_question` 在 hit / miss / 读降级 / 写失败
  四条路径上的**行为**与**日志字段**。

**为什么可用性那半必须显式测**：`make test` 里 `REDIS_URL` 一旦漏了 host 替换
（`.env` 里是 compose 服务名 `redis`），所有问答都会静默落到"读降级"。
而 `docs/tasks.md` 的 T16 验收方式是"看日志的 `cache` 字段"——
于是"B 权限问同一问题**不命中**"会因为**谁都不命中**而恒真。
这类假绿只能靠显式降级用例兜住；靠环境"碰巧连不上"不算覆盖。

不依赖真 Redis：读写往返用内存假 Redis、问答链路用 monkeypatch，
避免引入 fakeredis 新依赖（R12）。
"""

from __future__ import annotations

import json

import pytest
import redis

from app.core import cache
from app.core.cache import qa_cache_key
from app.repos.chunk_repo import ChunkHit
from app.services import qa
from app.services.generator import Citation, GeneratedAnswer
from app.services.permission import scope_fingerprint


def _qa_key(question: str, scope_keys: list[str]) -> str:
    return qa_cache_key(question, scope_fingerprint(scope_keys))


# ── 指纹本身 ──────────────────────────────────────────────


def test_fingerprint_order_insensitive() -> None:
    """同一组范围、不同顺序 → 同一指纹（缓存命中率的前提）。"""
    assert scope_fingerprint(["internal", "dept:销售部"]) == scope_fingerprint(
        ["dept:销售部", "internal"]
    )


def test_fingerprint_differs_by_scope() -> None:
    assert scope_fingerprint(["internal", "dept:销售部"]) != scope_fingerprint(
        ["internal", "dept:研发部"]
    )


def test_empty_scope_raises() -> None:
    """空范围必须显式失败：否则会生成一个"谁都能命中"的 key（R3/R7）。"""
    with pytest.raises(ValueError):
        scope_fingerprint([])


# ── 缓存 key ─────────────────────────────────────────────


def test_key_differs_by_scope() -> None:
    """★ 核心断言：A 权限与 B 权限提同一个问题，key 必须不同。"""
    q = "年假有几天？"
    assert _qa_key(q, ["internal", "dept:销售部"]) != _qa_key(
        q, ["internal", "dept:研发部"]
    )


def test_key_same_for_same_scope() -> None:
    """同一权限（顺序不同）提同一问题 → 同一 key（能正常命中缓存）。"""
    q = "年假有几天？"
    assert _qa_key(q, ["internal", "dept:销售部"]) == _qa_key(
        q, ["dept:销售部", "internal"]
    )


def test_key_differs_by_question() -> None:
    fp = scope_fingerprint(["internal"])
    assert qa_cache_key("年假有几天？", fp) != qa_cache_key("报销标准是多少？", fp)


def test_empty_question_raises() -> None:
    with pytest.raises(ValueError):
        qa_cache_key("   ", scope_fingerprint(["internal"]))


# ── 读写往返（假 Redis）──────────────────────────────────


class _FakeRedis:
    """内存版假 Redis：只实现缓存层用到的 get/set，不引入新依赖（R12）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


def test_get_set_roundtrip_without_real_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda: fake)

    key = _qa_key("年假有几天？", ["internal"])
    assert cache.get_answer(key) is None  # 未命中返回 None，不是错误

    payload = {"answer": "10 天", "refused": False, "citations": []}
    cache.set_answer(key, payload)
    assert cache.get_answer(key) == payload


def test_different_scope_does_not_see_cached_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把"越权"落到读写层验证：B 的范围读不到 A 写的内容。"""
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "get_redis", lambda: fake)

    key_a = _qa_key("年假有几天？", ["internal", "dept:销售部"])
    cache.set_answer(key_a, {"answer": "A 的答案", "refused": False, "citations": []})

    key_b = _qa_key("年假有几天？", ["internal", "dept:研发部"])
    assert cache.get_answer(key_b) is None


# ── 可用性四条路径（answer_question 层）──────────────────
#
# 上半证明"隔离有效"，这里证明"降级可见"——缺任何一半，T16 都可能是假绿。

_SCOPE = ["internal"]
_Q = "工作满10年年假多少天"
_ANSWER = "工作满 10 年，年假为 10 天[1]。"


def _hit() -> ChunkHit:
    return ChunkHit(
        chunk_id=11,
        content="员工工作满 10 年，年假为 10 天。",
        doc_id=1,
        doc_title="年假制度.txt",
        char_start=0,
        char_end=16,
        score=0.68,
    )


def _citation() -> Citation:
    return Citation(
        index=1,
        doc_id=1,
        doc_title="年假制度.txt",
        chunk_id=11,
        char_start=0,
        char_end=16,
        snippet="员工工作满 10 年，年假为 10 天。",
        score=0.68,
    )


def _gen() -> GeneratedAnswer:
    return GeneratedAnswer(
        answer=_ANSWER,
        refused=False,
        citations=[_citation()],
        prompt_tokens=30,
        completion_tokens=8,
        latency_ms=5,
    )


def _raise_redis_error(*args: object, **kwargs: object) -> None:
    """模拟 Redis 不可用：用真实的 redis 异常类型，不自造 Exception。"""
    raise redis.exceptions.ConnectionError(
        "Error 8 connecting to redis:6379. nodename nor servname provided."
    )


# 命中回放的载荷：与 `qa._cache_payload` 产出的**落盘契约**同格式。
# 这里独立写死，避免"用被测代码生成自己的输入"而互相掩盖。
_CACHED_PAYLOAD = {
    "answer": "（上次生成的答案）",
    "refused": False,
    "citations": [
        {
            "index": 1,
            "doc_id": 1,
            "doc_title": "年假制度.txt",
            "chunk_id": 11,
            "char_start": 0,
            "char_end": 16,
            "snippet": "员工工作满 10 年，年假为 10 天。",
            "score": 0.68,
        }
    ],
    "retrieved_chunk_ids": [11],
    "retrieve_ms": 40,
    "generate_ms": 900,
}


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch):
    """把嵌入 / 检索 / 生成换成假实现；缓存层留给各用例按需替换。"""
    monkeypatch.setattr(
        qa, "embed_texts", lambda texts, **kw: [[0.0] * 1024 for _ in texts]
    )
    monkeypatch.setattr(qa, "search_chunks", lambda vec, keys, top_k=5, **kw: [_hit()])
    monkeypatch.setattr(qa, "generate_answer", lambda question, hits, **kw: _gen())
    return monkeypatch


def _events(capsys) -> list[dict]:
    """取 stderr 里所有 JSON 日志事件（结构化日志都打到这里）。"""
    err = capsys.readouterr().err
    return [json.loads(ln) for ln in err.splitlines() if ln.strip().startswith("{")]


def _qa_record(events: list[dict]) -> dict:
    rows = [e for e in events if e.get("event") == "qa"]
    assert rows, f"没有 qa 事件：{events}"
    return rows[-1]


def _cache_records(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("event") == "cache"]


def test_cache_hit_replays_without_touching_models(chain, capsys) -> None:
    """① 命中：直接回放，分段耗时记 0，且**不调用**嵌入 / 检索 / 生成。"""

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("命中缓存时不得触碰嵌入 / 检索 / 生成")

    chain.setattr(qa, "get_answer", lambda key: dict(_CACHED_PAYLOAD))
    chain.setattr(qa, "embed_texts", boom)
    chain.setattr(qa, "search_chunks", boom)
    chain.setattr(qa, "generate_answer", boom)

    result = qa.answer_question(_Q, scope_keys=_SCOPE)

    assert result.answer == "（上次生成的答案）"
    assert result.retrieve_ms == 0
    assert result.generate_ms == 0
    assert [c.chunk_id for c in result.citations] == [11]

    events = _events(capsys)
    assert _qa_record(events)["cache"] == "hit"
    assert _cache_records(events) == []  # 命中路径不该产生降级日志


def test_cache_miss_runs_full_chain_and_writes(chain, capsys) -> None:
    """② 未命中：跑完整链路，`cache=miss`，并真的写了一次缓存。"""
    written: list[str] = []

    chain.setattr(qa, "get_answer", lambda key: None)
    chain.setattr(qa, "set_answer", lambda key, payload, **kw: written.append(key))

    result = qa.answer_question(_Q, scope_keys=_SCOPE)

    assert result.answer == _ANSWER
    assert len(written) == 1, "未命中时必须写一次缓存"

    record = _qa_record(_events(capsys))
    assert record["cache"] == "miss"
    assert "cache_write" not in record, "写成功时不该出现 cache_write 字段"


def test_cache_read_error_degrades_and_is_visible(chain, capsys) -> None:
    """③ 读缓存失败：问答**照常成功**，且 `cache=error`（而**不是** `miss`）。

    这条是 T16 假绿的解药——Redis 挂掉时若仍记 `miss`，
    就和"确实没缓存"分不开，"不同权限不命中"于是恒真。
    """
    chain.setattr(qa, "get_answer", _raise_redis_error)
    chain.setattr(qa, "set_answer", lambda key, payload, **kw: None)

    result = qa.answer_question(_Q, scope_keys=_SCOPE)

    # 不硬失败：缓存是纯性能优化，Redis 抖动不该让用户看到 5xx
    assert result.answer == _ANSWER
    assert result.refused is False

    events = _events(capsys)
    assert _qa_record(events)["cache"] == "error"

    cache_records = _cache_records(events)
    assert [e["op"] for e in cache_records] == ["get"]
    assert all(e["status"] == "error" for e in cache_records)


def test_cache_write_error_keeps_miss_and_flags_it(chain, capsys) -> None:
    """④ 读成功但写失败：`cache` 仍是 `miss`，另外标 `cache_write=error`。"""
    chain.setattr(qa, "get_answer", lambda key: None)
    chain.setattr(qa, "set_answer", _raise_redis_error)

    result = qa.answer_question(_Q, scope_keys=_SCOPE)

    assert result.answer == _ANSWER, "写缓存失败不得影响已产出的答案"

    events = _events(capsys)
    record = _qa_record(events)
    assert record["cache"] == "miss"
    assert record["cache_write"] == "error"
    assert [e["op"] for e in _cache_records(events)] == ["set"]
