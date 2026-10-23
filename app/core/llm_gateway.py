"""LLM 网关（V-1）：生成类模型调用的唯一出口（AGENTS.md R11）。

职责：超时、重试（指数退避）、可选备用模型降级、Token 记账。
业务代码禁止直连模型接口，一律 import 本模块的 chat()。

协议：OpenAI 兼容 /chat/completions——一个适配覆盖绝大多数厂商。
V-1 用 Ollama 本地 /v1 兼容层（qwen2.5）；将来换云端厂商
（DeepSeek/通义/GLM…）只改 .env 的 LLM_* 变量，代码不动。

与 embedding.py 的关系：V-1 嵌入调用暂不并入网关（T04 已验收、
只有一个调用方）；V0.1 换 TEI 时一并收敛。
熔断：V-1 不做——单进程内熔断无意义，需要跨进程状态（Redis），留到 V0.1。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

import httpx

# 只对这些情况重试：请求超时/限流/网关与服务端错误。
# 4xx 是我们自己的请求错了（如 key 无效、参数非法），重试只会白等。
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

_BACKOFF_BASE_S = 0.5  # 第 n 次重试前等待 0.5 * 2^(n-1) 秒：0.5s → 1s → 2s

# ── T32：Token 单价表（元 / 1K token） ────────────────────────────────────────
# 从 env LLM_TOKEN_PRICES 读（JSON 格式），内置默认空（本地 Ollama 无 API 费）。
# 格式示例：{"qwen-plus": {"input": 0.004, "output": 0.012}, ...}
# V0.1 嵌入模型固定本地 Ollama，不计费——这里只算生成层。
def _load_price_table() -> dict[str, dict[str, float]]:
    raw = os.getenv("LLM_TOKEN_PRICES", "").strip()
    if not raw:
        return {}
    try:
        table = json.loads(raw)
        if not isinstance(table, dict):
            return {}
        return table
    except json.JSONDecodeError:
        # R7：配置格式错了显式失败，不让静默吞掉
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "LLM_TOKEN_PRICES JSON 解析失败，成本计费禁用。"
        )
        return {}

_PRICE_TABLE: dict[str, dict[str, float]] = _load_price_table()


def _lookup_price(model_name: str) -> tuple[float, float] | None:
    """按 model 名匹配 (input_price_per_1k, output_price_per_1k) 或 None。

    匹配顺序：全匹配 → 前缀匹配（如 "qwen2.5:7b-instruct-q8_0" 匹配到 "qwen2.5:7b"）。
    None = 本地 Ollama 无 API 费，cost_cny 也将是 None。
    """
    if not _PRICE_TABLE:
        return None
    if model_name in _PRICE_TABLE:
        p = _PRICE_TABLE[model_name]
        return float(p.get("input", 0)), float(p.get("output", 0))
    # 前缀匹配：依次尝试缩短到 ":" 之前的部分
    parts = model_name.split(":")
    for i in range(len(parts), 0, -1):
        prefix = ":".join(parts[:i])
        if prefix in _PRICE_TABLE:
            p = _PRICE_TABLE[prefix]
            return float(p.get("input", 0)), float(p.get("output", 0))
    return None


def _compute_cost_cny(
    model_name: str, prompt_tokens: int, completion_tokens: int
) -> float | None:
    """按单价表算一次调用的成本（元）。无单价表返回 None。"""
    price = _lookup_price(model_name)
    if price is None:
        return None
    input_price, output_price = price
    input_cost = (prompt_tokens / 1000) * input_price
    output_cost = (completion_tokens / 1000) * output_price
    return round(input_cost + output_cost, 6)


class GatewayError(RuntimeError):
    """模型调用失败（R7：显式失败，调用方据此返回结构化错误，不得吞掉）。

    kind: timeout | connection | http_status | bad_response
    attempts: 本次失败已尝试的 HTTP 请求次数
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        attempts: int,
        status: int | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts
        self.status = status
        self.retryable = retryable


@dataclass
class ChatResult:
    """一次成功的生成调用结果（记账字段供 T32 延迟与成本埋点使用）。

    cost_cny=None = 无单价表（本地 Ollama 免费）；有单价表时为 float。
    """

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int  # 从 chat() 进入到成功的总耗时（含重试与降级等待）
    attempts: int  # 本次 chat() 实际发出的 HTTP 请求总次数（含主与备用）
    cost_cny: float | None = None


@dataclass
class _ProviderConfig:
    base_url: str
    api_key: str
    model: str


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _load_fallback() -> _ProviderConfig | None:
    """读备用模型配置；LLM_FALLBACK_MODEL 留空 = 禁用降级。"""
    model = os.getenv("LLM_FALLBACK_MODEL", "").strip()
    if not model:
        return None
    return _ProviderConfig(
        base_url=os.getenv("LLM_FALLBACK_BASE_URL", "")
        or os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.getenv("LLM_FALLBACK_API_KEY", "") or os.getenv("LLM_API_KEY", ""),
        model=model,
    )


def chat(
    messages: list[dict[str, str]],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 1024,
    timeout_s: float | None = None,
    max_retries: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ChatResult:
    """生成一次对话补全（OpenAI 兼容协议，非流式）。

    Args:
        messages: OpenAI 格式消息，如 [{"role": "user", "content": "..."}]。
        base_url / api_key / model: 覆盖 .env 主配置（测试或临时切换用）。
        temperature: 固定低温——RAG 答案要贴资料，不要发散。
        max_tokens: 生成上限。
        timeout_s: 单次请求超时，默认 LLM_TIMEOUT_S（本地 7B 推理慢，120s）。
        max_retries: 重试次数（不含首次），默认 LLM_MAX_RETRIES（2）。
        transport: 仅测试注入 httpx.MockTransport 用，业务代码不要传。

    Returns:
        ChatResult（含 Token 记账）。响应缺 usage 时抛 GatewayError——
        静默记 0 会污染成本统计（R7）。

    Raises:
        GatewayError: 重试（含备用模型）耗尽或遇到不可恢复错误。
    """
    primary = _ProviderConfig(
        base_url=base_url
        if base_url is not None
        else os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
        api_key=api_key if api_key is not None else os.getenv("LLM_API_KEY", ""),
        model=model
        if model is not None
        else os.getenv("LLM_MODEL", "qwen2.5:7b-instruct-q8_0"),
    )
    fallback = _load_fallback()
    if fallback is not None and fallback == primary:
        fallback = None  # 备用与主配置相同 = 没有降级意义，避免把同样的错重试两遍

    # 超时按 provider 配置，不给全局硬默认值（.trae/rules/rag-core.md 第六节）。
    #
    # 为什么这里默认 120s 而不是 15s：
    #   V-1 主力是本地 Ollama qwen2.5-7B（CPU 推理，q8），实测单次生成 14—26s，
    #   15s 会直接超时。而 15s 是"云端 chat API"的经验值——把它的经验值套到
    #   本地大模型上，等于把一种 provider 的配置硬当成所有 provider 的默认。
    #
    # 各 provider 的推荐取值：
    #   云端 chat API（DeepSeek / 通义 / GLM 等）：read 15—30s
    #     （首 token 快，但长答案读流要时间）
    #   本地 GPU 推理（vLLM 等）：30—60s
    #   本地 CPU 推理（本项目 V-1）：120—300s
    #
    # 企业级应升级为（V0.1 起，配合 ADR-0007 的"配置可管理"）：
    #   ① 超时与 provider/模型一起存配置（V1 的表 + CRUD），而不是写死在 env 默认值；
    #   ② 拆成 connect_timeout 与 read_timeout 两个值——连上慢和算得慢是两类问题，
    #      一个总超时分不清该调哪个；
    #   ③ 流式生成用「首 token 超时」（10—15s）+ 总时长上限：模型"卡住"能快速发现，
    #      不必傻等满总超时；
    #   ④ 总超时必须计入重试预算：最坏耗时 ≈ 超时 ×（重试次数 + 1），
    #      本例 120s × 3 = 360s——所以必须配合网关并发限流，否则少数慢请求会吃光配额。
    timeout = (
        timeout_s if timeout_s is not None else float(os.getenv("LLM_TIMEOUT_S", "120"))
    )
    retries = (
        max_retries if max_retries is not None
        else int(os.getenv("LLM_MAX_RETRIES", "2"))
    )

    configs = [primary] + ([] if fallback is None else [fallback])
    started = time.monotonic()
    prior_attempts = 0
    last_error: GatewayError | None = None
    for cfg in configs:
        try:
            result = _chat_with_retry(
                cfg, messages,
                temperature=temperature, max_tokens=max_tokens,
                timeout_s=timeout, max_retries=retries, transport=transport,
            )
        except GatewayError as exc:
            last_error = exc
            prior_attempts += exc.attempts
            continue
        result.attempts += prior_attempts
        result.latency_ms = int((time.monotonic() - started) * 1000)
        result.cost_cny = _compute_cost_cny(
            result.model, result.prompt_tokens, result.completion_tokens
        )
        return result
    assert last_error is not None  # configs 非空，循环体必然赋值
    raise GatewayError(
        f"模型调用最终失败（共尝试 {prior_attempts} 次，含备用模型）：{last_error}",
        kind=last_error.kind,
        attempts=prior_attempts,
        status=last_error.status,
        retryable=False,
    )


def _chat_with_retry(
    cfg: _ProviderConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    max_tokens: int,
    timeout_s: float,
    max_retries: int,
    transport: httpx.BaseTransport | None,
) -> ChatResult:
    """对单个模型配置做「首次 + max_retries 次重试」。"""
    last_error: GatewayError | None = None
    attempt = 1
    for attempt in range(1, max_retries + 2):
        if last_error is not None:
            _sleep(_BACKOFF_BASE_S * (2 ** (attempt - 2)))
        try:
            content, (prompt_t, completion_t) = _call_once(
                cfg, messages, temperature, max_tokens, timeout_s, transport
            )
        except GatewayError as exc:
            last_error = exc
            if not exc.retryable:
                break
            continue
        return ChatResult(
            content=content,
            model=cfg.model,
            prompt_tokens=prompt_t,
            completion_tokens=completion_t,
            total_tokens=prompt_t + completion_t,
            latency_ms=0,  # 由 chat() 统一计总耗时
            attempts=attempt,
        )
    assert last_error is not None  # 只有 return 能正常离开循环体
    raise GatewayError(
        f"模型 {cfg.model} 调用失败（共 {attempt} 次尝试）：{last_error}",
        kind=last_error.kind,
        attempts=attempt,
        status=last_error.status,
        retryable=False,
    )


def _call_once(
    cfg: _ProviderConfig,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout_s: float,
    transport: httpx.BaseTransport | None,
) -> tuple[str, tuple[int, int]]:
    """发一次请求并解析响应（R7：每类失败都显式抛错，不吞不兜底）。"""
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    try:
        # trust_env=False：与 embedding.py 同理——本机/内网服务绝不能走系统代理，
        # 否则 Clash 等代理会把 localhost 请求转发出去
        with httpx.Client(
            timeout=timeout_s, trust_env=False, transport=transport
        ) as client:
            resp = client.post(
                f"{cfg.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
    except httpx.TimeoutException as exc:
        raise GatewayError(
            f"模型请求超时（>{timeout_s}s）：{cfg.base_url}（model={cfg.model}）",
            kind="timeout",
            attempts=1,
        ) from exc
    except httpx.HTTPError as exc:
        raise GatewayError(
            f"模型服务不可达：{cfg.base_url}（{exc!r}）。"
            "请确认服务已启动。若你是故意改错地址做验收——看到这条即说明网关按预期工作。",
            kind="connection",
            attempts=1,
        ) from exc

    if resp.status_code != 200:
        raise GatewayError(
            f"模型返回 HTTP {resp.status_code}：{resp.text[:300]}",
            kind="http_status",
            attempts=1,
            status=resp.status_code,
            retryable=resp.status_code in _RETRYABLE_STATUS,
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise GatewayError(
            f"模型响应不是合法 JSON：{resp.text[:300]}",
            kind="bad_response",
            attempts=1,
            retryable=False,
        ) from exc

    choices = data.get("choices")
    content = ""
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
    if not content.strip():
        raise GatewayError(
            f"模型响应缺少 choices[0].message.content：{str(data)[:300]}",
            kind="bad_response",
            attempts=1,
            retryable=False,
        )

    usage = data.get("usage") or {}
    try:
        prompt_t = int(usage["prompt_tokens"])
        completion_t = int(usage["completion_tokens"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GatewayError(
            "模型响应缺少 usage 记账字段（prompt_tokens/completion_tokens），"
            f"拒绝返回以免污染成本统计：{str(usage)[:200]}",
            kind="bad_response",
            attempts=1,
            retryable=False,
        ) from exc
    return content, (prompt_t, completion_t)
