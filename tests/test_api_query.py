"""POST /api/v1/query 接口测试（T08）。

全部 mock 掉嵌入 / 检索 / 生成，不联网、不需要真模型与数据库。
覆盖验收：① curl 提问能拿到 JSON；② 超长问题返回 400【且不调用模型】。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.v1.query import _require_user_scope_keys
from app.core.llm_gateway import GatewayError
from app.main import app
from app.repos.chunk_repo import ChunkHit
from app.services import qa
from app.services.generator import REFUSAL_TEXT, Citation, GeneratedAnswer

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_dependency_overrides():
    """每个用例后清空依赖覆盖，避免相互污染（401 用例需要跑真实依赖）。"""
    yield
    app.dependency_overrides.clear()


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


def _patch(monkeypatch, gen) -> None:
    """把 qa 依赖的三步全部替换为假实现，并把鉴权依赖固定为 ["internal"]。

    T15 起 /query 需要 `X-KB-User` 解析权限；本文件是纯接口层测试（不连库），
    故用 dependency_overrides 让鉴权直接返回固定范围，绕开数据库。
    """

    def fake_embed(texts, **kwargs):
        return [[0.0] * 1024 for _ in texts]

    def fake_search(query_vec, scope_keys, top_k=5, **kwargs):
        return [_hit()]

    app.dependency_overrides[_require_user_scope_keys] = lambda: ["internal"]
    monkeypatch.setattr(qa, "embed_texts", fake_embed)
    monkeypatch.setattr(qa, "search_chunks", fake_search)
    monkeypatch.setattr(qa, "generate_answer", gen)


def test_query_returns_json(monkeypatch):
    """验收①：提问能拿到契约规定的 JSON。"""

    def fake_gen(question, hits, **kwargs):
        return GeneratedAnswer(
            answer="工作满 10 年，年假为 10 天[1]。",
            refused=False,
            citations=[
                Citation(
                    index=1,
                    doc_id=1,
                    doc_title="年假制度.txt",
                    chunk_id=11,
                    char_start=0,
                    char_end=16,
                    snippet="员工工作满 10 年，年假为 10 天。",
                    score=0.68,
                )
            ],
            prompt_tokens=30,
            completion_tokens=8,
            latency_ms=5,
        )

    _patch(monkeypatch, fake_gen)
    resp = client.post("/api/v1/query", json={"question": "工作满10年年假多少天"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["refused"] is False
    assert body["degraded"] is False
    assert body["answer"] == "工作满 10 年，年假为 10 天[1]。"
    assert body["citations"][0]["chunk_id"] == 11
    assert body["citations"][0]["doc_title"] == "年假制度.txt"
    assert isinstance(body["latency_ms"], int)
    assert "retrieve_ms" in body and "generate_ms" in body


def test_refusal_returns_200_not_4xx(monkeypatch):
    """拒答是正常业务结果 → 200，且 answer 恰为固定句式（R5）。"""

    def fake_gen(question, hits, **kwargs):
        return GeneratedAnswer(
            answer=REFUSAL_TEXT,
            refused=True,
            citations=[],
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=3,
        )

    _patch(monkeypatch, fake_gen)
    resp = client.post("/api/v1/query", json={"question": "公司今年年会在哪里举办"})

    assert resp.status_code == 200  # 绝不能是 4xx/5xx
    body = resp.json()
    assert body["refused"] is True
    assert body["answer"] == REFUSAL_TEXT
    assert body["citations"] == []


@pytest.mark.parametrize(
    "question",
    ["长" * 2001, "   "],
    ids=["too_long", "blank"],
)
def test_invalid_question_400_without_model_call(monkeypatch, question):
    """验收②：非法问题（超长/空白）返回 400 + QUESTION_INVALID，且不调用任何模型。"""

    def boom(*args, **kwargs):
        raise AssertionError("参数校验必须在调用嵌入/检索/生成之前失败")

    # 鉴权依赖先放行（否则缺失 header 会先返回 401，掩盖参数校验路径）
    app.dependency_overrides[_require_user_scope_keys] = lambda: ["internal"]
    monkeypatch.setattr(qa, "embed_texts", boom)
    monkeypatch.setattr(qa, "search_chunks", boom)
    monkeypatch.setattr(qa, "generate_answer", boom)

    resp = client.post("/api/v1/query", json={"question": question})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "QUESTION_INVALID"


def test_missing_user_header_returns_401():
    """T15：未提供 X-KB-User → 401 UNAUTHENTICATED（security.md 2.2）。

    本用例不覆盖鉴权依赖，走真实依赖；缺 header 时在查库前即显式失败（R7）。
    """
    resp = client.post("/api/v1/query", json={"question": "年假多少天"})

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


def test_gateway_error_maps_to_503(monkeypatch):
    """模型挂了 → 503 + MODEL_UNAVAILABLE（R7 显式失败，不返回空答案冒充成功）。"""

    def fake_gen(question, hits, **kwargs):
        raise GatewayError("connection refused", kind="connection", attempts=2)

    _patch(monkeypatch, fake_gen)
    resp = client.post("/api/v1/query", json={"question": "年假多少天"})

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "MODEL_UNAVAILABLE"
