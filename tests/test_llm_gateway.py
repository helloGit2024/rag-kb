"""LLM 网关单元测试：全部走 httpx.MockTransport，不联网、不需要真模型。

覆盖 T06 验收关注点：改错地址（连接失败）时重试后抛 GatewayError，
而不是崩溃或静默返回。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core import llm_gateway
from app.core.llm_gateway import GatewayError, chat


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """单测不真睡（重试退避）+ 隔离 .env 里可能的降级配置。"""
    monkeypatch.setattr(llm_gateway, "_sleep", lambda _s: None)
    monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)


def _ok(content="答案内容", usage=None):
    body = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def test_success_records_usage():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _ok("你好", {"prompt_tokens": 10, "completion_tokens": 5})

    r = chat(
        [{"role": "user", "content": "hi"}],
        base_url="http://mock", api_key="k", model="m",
        transport=httpx.MockTransport(handler),
    )

    assert r.content == "你好"
    assert (r.prompt_tokens, r.completion_tokens, r.total_tokens) == (10, 5, 15)
    assert r.attempts == 1
    assert r.model == "m"
    # OpenAI 兼容请求体
    assert seen[0]["model"] == "m"
    assert seen[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert seen[0]["stream"] is False
    assert seen[0]["max_tokens"] == 1024


def test_success_sends_bearer():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return _ok("好", {"prompt_tokens": 1, "completion_tokens": 1})

    chat(
        [{"role": "user", "content": "hi"}],
        base_url="http://mock", api_key="secret", model="m",
        transport=httpx.MockTransport(handler),
    )
    assert captured["auth"] == "Bearer secret"


def test_retry_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, text="boom")
        return _ok("恢复了", {"prompt_tokens": 1, "completion_tokens": 1})

    r = chat(
        [{"role": "user", "content": "q"}],
        base_url="http://mock", api_key="", model="m", max_retries=2,
        transport=httpx.MockTransport(handler),
    )
    assert calls["n"] == 3
    assert r.attempts == 3
    assert r.content == "恢复了"


def test_retries_exhausted_graceful_error():
    """对应 T06 验收：一直失败 → 重试耗尽 → 结构化报错，不崩。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="overloaded")

    with pytest.raises(GatewayError) as ei:
        chat(
            [{"role": "user", "content": "q"}],
            base_url="http://mock", api_key="", model="m", max_retries=2,
            transport=httpx.MockTransport(handler),
        )
    assert calls["n"] == 3  # 首次 + 2 次重试
    assert ei.value.attempts == 3
    assert ei.value.kind == "http_status"
    assert ei.value.status == 503


def test_client_error_no_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(GatewayError) as ei:
        chat(
            [{"role": "user", "content": "q"}],
            base_url="http://mock", api_key="wrong", model="m",
            transport=httpx.MockTransport(handler),
        )
    assert calls["n"] == 1  # 4xx 是我们自己的错，重试无意义
    assert ei.value.status == 401


def test_connection_error_retries_then_raises():
    """改错地址就是这个错误类型：连接失败 → 重试 → GatewayError。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(GatewayError) as ei:
        chat(
            [{"role": "user", "content": "q"}],
            base_url="http://wrong-host", api_key="", model="m", max_retries=1,
            transport=httpx.MockTransport(handler),
        )
    assert calls["n"] == 2
    assert ei.value.kind == "connection"
    assert ei.value.attempts == 2
    assert "不可达" in str(ei.value)


def test_missing_usage_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok("答案", usage=None)  # 响应缺 usage 记账

    with pytest.raises(GatewayError) as ei:
        chat(
            [{"role": "user", "content": "q"}],
            base_url="http://mock", api_key="", model="m",
            transport=httpx.MockTransport(handler),
        )
    assert ei.value.kind == "bad_response"
    assert "usage" in str(ei.value)


def test_fallback_takes_over(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "http://fallback")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "fk")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "m-fallback")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == "m-fallback":
            return _ok("备用接管", {"prompt_tokens": 2, "completion_tokens": 3})
        return httpx.Response(500, text="primary down")

    r = chat(
        [{"role": "user", "content": "q"}],
        base_url="http://primary", api_key="pk", model="m-primary", max_retries=1,
        transport=httpx.MockTransport(handler),
    )
    assert r.content == "备用接管"
    assert r.model == "m-fallback"
    assert r.attempts == 3  # 主模型 2 次（首次+1 重试）+ 备用 1 次
