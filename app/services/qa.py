"""问答编排（V-1）：嵌入 → 检索 → 生成。

本层是业务编排层（目录约定）：
  - 不写 SQL（在 app/repos/）；
  - 不拼提示词（在 app/prompts/ + app/services/generator）；
  - 模型调用一律经 app/core/llm_gateway（R11）。
职责是把三步串起来并分段计时，供 T32 的延迟/成本埋点复用。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.core.cache import (
    CACHE_ERROR,
    CACHE_HIT,
    CACHE_MISS,
    CACHE_UNAVAILABLE_ERRORS,
    get_answer,
    qa_cache_key,
    set_answer,
)
from app.core.embedding import embed_texts
from app.repos.chunk_repo import search_chunks
from app.services.generator import Citation, generate_answer
from app.services.permission import scope_fingerprint


@dataclass
class QaResult:
    """一次问答的完整结果（引用为 generator.Citation，由 API 层映射成契约字段）。"""

    answer: str
    refused: bool
    citations: list[Citation]
    retrieve_ms: int
    generate_ms: int
    latency_ms: int


def answer_question(
    question: str,
    *,
    top_k: int = 5,
    scope_keys: list[str] | None = None,
) -> QaResult:
    """把问题跑完整条链路，外面套一层**权限隔离**缓存（T16）。

    Args:
        question: 用户问题（调用方应已校验非空/未超长）。
        top_k: 检索条数。
        scope_keys: 用户可见范围。★ 必须显式传入，权限过滤在 SQL 内完成（R2）。

    Returns:
        QaResult。命中缓存时直接回放上次结果——此时 `retrieve_ms` / `generate_ms`
        记 0（这次没有真的嵌入/检索/生成），`latency_ms` 只含缓存读取耗时。

    Raises:
        ValueError: question 为空或 scope_keys 为空（R7 显式失败）。
        EmbeddingError / GatewayError: 模型调用失败，原样上抛由 API 层转结构化错误码。
    """
    if not question.strip():
        raise ValueError("question 不能为空（R7）")
    if not scope_keys:
        # R7：宁可显式失败，也不静默检索出越权内容
        raise ValueError("scope_keys 不能为空（R2/R7）")

    # R3：缓存 key = 问题文本 + 权限指纹。指纹不同 → key 不同 → 绝不跨权限命中。
    key = qa_cache_key(question, scope_fingerprint(scope_keys))

    # ① 查缓存：命中就直接回放，不触碰嵌入/检索/生成（省时省钱）
    t_cache = time.perf_counter()
    cached, cache_status = _read_cache(key)
    if cached is not None:
        latency_ms = round((time.perf_counter() - t_cache) * 1000)
        result = QaResult(
            answer=cached["answer"],
            refused=cached["refused"],
            citations=[Citation(**c) for c in cached["citations"]],
            retrieve_ms=0,
            generate_ms=0,
            # 命中分支只含缓存读取耗时；线上口径（含命中）不得与评估口径
            # 相加或平均——评估口径恒为未命中（docs/glossary.md 第七节）
            latency_ms=latency_ms,
        )
        _log_qa(
            question,
            scope_keys,
            result,
            cached["retrieved_chunk_ids"],
            cache_status=cache_status,
        )
        return result

    # ② 未命中：跑完整链路
    t0 = time.perf_counter()
    query_vec = embed_texts([question])[0]
    hits = search_chunks(query_vec, scope_keys, top_k=top_k)
    t1 = time.perf_counter()
    gen = generate_answer(question, hits)
    t2 = time.perf_counter()

    retrieve_ms = round((t1 - t0) * 1000)
    generate_ms = round((t2 - t1) * 1000)
    result = QaResult(
        answer=gen.answer,
        refused=gen.refused,
        citations=gen.citations,
        retrieve_ms=retrieve_ms,
        generate_ms=generate_ms,
        latency_ms=retrieve_ms + generate_ms,
    )

    retrieved_chunk_ids = [h.chunk_id for h in hits]
    write_ok = _write_cache(key, _cache_payload(result, retrieved_chunk_ids))
    _log_qa(
        question,
        scope_keys,
        result,
        retrieved_chunk_ids,
        cache_status=cache_status,
        cache_write_ok=write_ok,
    )

    return result


def _cache_payload(result: QaResult, retrieved_chunk_ids: list[int]) -> dict[str, Any]:
    """把结果拍成可 JSON 序列化的缓存载荷（引用逐字段存，回放时重建 Citation）。"""
    return {
        "answer": result.answer,
        "refused": result.refused,
        "citations": [asdict(c) for c in result.citations],
        "retrieved_chunk_ids": list(retrieved_chunk_ids),
        "retrieve_ms": result.retrieve_ms,
        "generate_ms": result.generate_ms,
    }


def _read_cache(key: str) -> tuple[dict[str, Any] | None, str]:
    """读缓存，并返回**这次读取的状态**——它就是 `cache` 日志字段的取值。

    为什么不直接放行异常：缓存是纯性能优化（可删），Redis 抖动不应让用户看到 500；
    为什么仍要记日志、且要把状态**带回给调用方**：R7 要求"错得可见"，不静默吞掉。
    只记在另一条 `event=cache` 日志里不够——那条日志与本次问答没有共同字段可比对，
    于是问答日志里"缓存不可用"和"确实没缓存"长得一模一样，T16 的验收会因此假绿。

    Returns:
        `(payload, status)`：status ∈ {`hit`, `miss`, `error`}；
        payload 仅在 status 为 `hit` 时非 None。
    """
    try:
        payload = get_answer(key)
    except CACHE_UNAVAILABLE_ERRORS as exc:
        _log_cache_error("get", exc)
        return None, CACHE_ERROR
    if payload is None:
        return None, CACHE_MISS
    return payload, CACHE_HIT


def _write_cache(key: str, payload: dict[str, Any]) -> bool:
    """写缓存；返回是否写成功。

    失败**不影响本次问答已产出的答案**（缓存可删），但调用方会据此在问答日志里
    补一个 `cache_write=error`：写入失败是完全静默的，少了这个字段就不可统计。
    """
    try:
        set_answer(key, payload)
    except CACHE_UNAVAILABLE_ERRORS as exc:
        _log_cache_error("set", exc)
        return False
    return True


def _log_cache_error(op: str, exc: Exception) -> None:
    """缓存故障的结构化日志（detail 截断，避免把 DSN/内网地址带进日志——R1）。"""
    print(
        json.dumps(
            {"event": "cache", "op": op, "status": "error", "detail": str(exc)[:200]},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )


def _log_qa(
    question: str,
    scope_keys: list[str],
    result: QaResult,
    retrieved_chunk_ids: list[int],
    *,
    cache_status: str,
    cache_write_ok: bool = True,
) -> None:
    """落一行结构化问答记录。

    rag-core「生成」规则要求每条问答都有结构化记录；V-1 还没有 query_logs 表
    （T02 只建了 documents/chunks），因此先打印一行 JSON，落表留到 V0.1 T34（已入账）。
    （T34 = query_logs 问答流水入库；T32 是埋点、T33 是评估入库，均不是落表。）
    只记 chunk_id 不记块文本——避免把全文文档内容写入日志（api-data 规则第五节）。
    ★ 把**当时生效的可见范围**一并记入，字段名固定为 `scope_snapshot`（值 = 参数
      scope_keys）：security.md 2.2 要求每次问答都能事后定位"当时他到底能看到什么"。
      与 `query_logs.scope_snapshot`（T34 落表）共用同一个名字，落表时无需再做名字映射。
      四个易混词的区分见 docs/glossary.md 第七节 —— scope_key（单个标签）/
      scope_keys（内存里的值）/ scope_snapshot（落盘字段名）/
      缓存指纹（hash，局部变量）。
    ★ `cache` 字段 = `hit` / `miss` / `error`（取值见 app/core/cache.py，T16 的验收
      就是看它）：不同权限问同一问题必须是 `miss`。
      **`miss` 与 `error` 必须分开**——`miss` 是"读成功、确实没缓存"，`error` 是
      "Redis 不可用、根本没读成"。混成一个值会让验收假绿：Redis 挂掉时谁都不命中，
      "不同权限不命中"就恒真了。
    ★ `cache_write` 字段只在**写缓存失败**时出现（值恒为 `error`）：写失败完全静默，
      不显式标注就不可统计。
    """
    record: dict[str, Any] = {
        "event": "qa",
        "question": question,
        "cache": cache_status,
        "scope_snapshot": list(scope_keys),
        "retrieved_chunk_ids": list(retrieved_chunk_ids),
        "cited_chunk_ids": [c.chunk_id for c in result.citations],
        "refused": result.refused,
        "degraded": False,
        "retrieve_ms": result.retrieve_ms,
        "generate_ms": result.generate_ms,
        "latency_ms": result.latency_ms,
    }
    # 只在失败时出现：字段"多出来"本身就是信号，正常路径不背这个噪音。
    if not cache_write_ok:
        record["cache_write"] = CACHE_ERROR
    print(
        json.dumps(record, ensure_ascii=False),
        file=sys.stderr,
    )
