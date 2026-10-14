"""答案生成器单元测试：mock LLM 网关，不联网、不需要真模型。

覆盖 T07 验收：① 能答的问题带 [n] 且引用对得上；② 无答案时恰好输出固定句式。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core import llm_gateway
from app.core.llm_gateway import GatewayError
from app.repos.chunk_repo import ChunkHit
from app.services.generator import (
    REFUSAL_TEXT,
    generate_answer,
    load_prompt_template,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """单测不真睡 + 隔离 .env 里可能的降级配置。"""
    monkeypatch.setattr(llm_gateway, "_sleep", lambda _s: None)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)


def _hits() -> list[ChunkHit]:
    return [
        ChunkHit(
            chunk_id=11,
            content="员工工作满 10 年，年假为 10 天。",
            doc_id=1,
            doc_title="年假制度.txt",
            char_start=0,
            char_end=16,
            score=0.68,
        ),
        ChunkHit(
            chunk_id=12,
            content="工作满 5 年，年假为 15 天。",
            doc_id=1,
            doc_title="年假制度.txt",
            char_start=16,
            char_end=32,
            score=0.61,
        ),
        ChunkHit(
            chunk_id=21,
            content="年假需提前三个工作日申请。",
            doc_id=2,
            doc_title="请假流程.txt",
            char_start=0,
            char_end=14,
            score=0.52,
        ),
    ]


def _mock(content: str, seen: list | None = None, usage=(30, 8)):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
            },
        )

    return httpx.MockTransport(handler)


def test_answer_binds_citation():
    seen: list = []
    r = generate_answer(
        "工作满 10 年年假多少天？",
        _hits(),
        transport=_mock("工作满 10 年，年假为 10 天[1]。", seen),
    )

    assert r.refused is False
    assert len(r.citations) == 1
    c = r.citations[0]
    assert c.index == 1
    assert c.chunk_id == 11
    assert c.doc_id == 1
    assert c.doc_title == "年假制度.txt"
    assert c.char_start == 0 and c.char_end == 16
    assert c.snippet == "员工工作满 10 年，年假为 10 天。"
    assert (r.prompt_tokens, r.completion_tokens) == (30, 8)

    # 提示词里资料应带编号与出处标题，问题已填入
    sent = seen[0]["messages"][0]["content"]
    assert "[1]（来源：《年假制度.txt》）" in sent
    assert "[3]（来源：《请假流程.txt》）" in sent
    assert "{{context}}" not in sent and "{{question}}" not in sent
    assert "工作满 10 年年假多少天？" in sent


def test_citations_follow_appearance_order():
    r = generate_answer(
        "年假怎么算？",
        _hits(),
        transport=_mock("满 5 年 15 天[2]，满 10 年 10 天[1]。"),
    )
    assert [c.index for c in r.citations] == [2, 1]
    assert [c.chunk_id for c in r.citations] == [12, 11]


def test_exact_refusal():
    r = generate_answer("公司年会在哪开？", _hits(), transport=_mock(REFUSAL_TEXT))
    assert r.refused is True
    assert r.answer == REFUSAL_TEXT
    assert r.citations == []


def test_refusal_with_extra_text_is_normalized(capsys):
    r = generate_answer(
        "公司年会在哪开？",
        _hits(),
        transport=_mock("抱歉，我查了资料。\n" + REFUSAL_TEXT),
    )
    assert r.refused is True
    assert r.answer == REFUSAL_TEXT  # 纠偏为唯一固定句式（R5）
    assert "已归一为固定句式" in capsys.readouterr().err


def test_empty_hits_skips_model():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("hits 为空时不应调用模型")

    r = generate_answer("年假多少天？", [], transport=httpx.MockTransport(handler))
    assert r.refused is True
    assert r.answer == REFUSAL_TEXT
    assert (r.prompt_tokens, r.completion_tokens, r.latency_ms) == (0, 0, 0)


def test_invalid_index_ignored(capsys):
    r = generate_answer(
        "年假多少天？", _hits(), transport=_mock("年假 10 天[1]，见[9]。")
    )
    assert [c.index for c in r.citations] == [1]
    assert "不存在的编号 [9]" in capsys.readouterr().err


def test_missing_citation_warns(capsys):
    r = generate_answer("年假多少天？", _hits(), transport=_mock("年假为 10 天。"))
    assert r.refused is False
    assert r.citations == []
    assert "未包含有效引用编号" in capsys.readouterr().err


def test_rewritten_number_refused(capsys):
    """答案数字在原文中找不到（如把 10 天改成 12 天）→ 按可疑拒答（R6）。"""
    r = generate_answer(
        "工作满 10 年年假多少天？",
        _hits(),
        transport=_mock("工作满 10 年，年假为 12 天[1]。"),
    )
    assert r.refused is True
    assert r.answer == REFUSAL_TEXT
    assert r.citations == []
    assert "找不到的数字 ['12']" in capsys.readouterr().err


def test_faithful_number_passes():
    """原文照抄的数字（10 天）→ 正常返回，不误拒。"""
    r = generate_answer(
        "工作满 10 年年假多少天？",
        _hits(),
        transport=_mock("工作满 10 年，年假为 10 天[1]。"),
    )
    assert r.refused is False
    assert len(r.citations) == 1


def test_prompt_template_contract():
    tpl = load_prompt_template()
    assert REFUSAL_TEXT in tpl
    assert "{{context}}" in tpl
    assert "{{question}}" in tpl
    assert "原文照抄" in tpl  # R6


def test_gateway_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad key")

    with pytest.raises(GatewayError):
        generate_answer("年假多少天？", _hits(), transport=httpx.MockTransport(handler))


def test_empty_question_rejected():
    with pytest.raises(ValueError):
        generate_answer("   ", _hits())
