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
from app.repos import query_log_repo
from app.repos.chunk_repo import ChunkHit, search_chunks
from app.services.generator import Citation, generate_answer
from app.services.permission import scope_fingerprint


@dataclass
class QaResult:
    """一次问答的完整结果（引用为 generator.Citation，由 API 层映射成契约字段）。

    prompt_tokens / completion_tokens 仅 run_chain（无缓存路径）填真实值；
    缓存命中回放时为 0——命中分支没有真的调用模型（T32 成本埋点以此为据）。
    cost_cny=None 同 prompt_tokens/completion_tokens——缓存命中时为 None。
    """

    answer: str
    refused: bool
    citations: list[Citation]
    retrieve_ms: int
    generate_ms: int
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model_used: str | None = None
    cost_cny: float | None = None
    # T34：本次问答在 query_logs 里的行 id；未落库（无身份/落库失败）为 None
    query_log_id: int | None = None


def run_chain(
    question: str,
    *,
    top_k: int = 5,
    scope_keys: list[str],
    kb_ids: list[int] | None = None,
) -> tuple[QaResult, list[ChunkHit]]:
    """无缓存跑完整链路（嵌入 → 检索 → 生成），返回结果与检索命中块。

    ★ 评估口径要求"无缓存命中"（.trae/rules/eval.md 第四节）：T28 的评估
      编排直接调本函数；answer_question 的未命中分支同样走这里——
      在线与评估共用一条链，不分叉。
    ★ 参数校验由调用方负责（与 answer_question 同一约定）。
    """
    t0 = time.perf_counter()
    query_vec = embed_texts([question])[0]
    hits = search_chunks(query_vec, scope_keys, top_k=top_k, kb_ids=kb_ids)
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
        prompt_tokens=gen.prompt_tokens,
        completion_tokens=gen.completion_tokens,
        model_used=gen.model_used,
        cost_cny=gen.cost_cny,
    )
    return result, hits


def answer_question(
    question: str,
    *,
    top_k: int = 5,
    scope_keys: list[str] | None = None,
    kb_ids: list[int] | None = None,
    user_id: int | None = None,
) -> QaResult:
    """把问题跑完整条链路，外面套一层**权限隔离**缓存（T16 + T17b）。

    Args:
        question: 用户问题（调用方应已校验非空/未超长）。
        top_k: 检索条数。
        scope_keys: 用户可见范围（非 kb 维度）。
            ★ 必须显式传入，权限过滤在 SQL 内完成（R2）。
        kb_ids: 用户可见知识库 ID 列表（T17b）。非空时检索 SQL 追加 kb 轴条件。
        user_id: 当前用户主键（T34）。非 None 时本次问答会写一行 query_logs；
            脚本/测试不传则只打 stderr JSON（日志落表只对线上带身份的请求生效）。

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

    # R3 + T17b：缓存 key = 问题哈希 + 权限指纹（含 kb_ids）。
    # 指纹不同 → key 不同 → 绝不跨权限命中——包括"同 scope_keys 但不同 kb_ids"
    # 这种 kb 轴越权，都能被 kb_ids 拼进哈希挡住。
    key = qa_cache_key(question, scope_fingerprint(scope_keys, kb_ids))

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
            user_id=user_id,
            kb_ids=kb_ids,
        )
        return result

    # ② 未命中：跑完整链路（与评估共用的无缓存路径）
    result, hits = run_chain(
        question, top_k=top_k, scope_keys=scope_keys, kb_ids=kb_ids
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
        user_id=user_id,
        kb_ids=kb_ids,
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
    user_id: int | None = None,
    kb_ids: list[int] | None = None,
) -> None:
    """记一行结构化问答记录：stderr JSON + （带身份时）query_logs 落表。

    T34 起 `query_logs` 已建表：带 user_id 的线上请求额外写一行（旁路记录）。
    **落表失败不影响问答返回**——日志不是业务结果，失败只显式打 stderr，
    绝不把用户请求变成 500（与 R7 不冲突：错误仍然可见，只是不阻断服务）。
    只记 chunk_id 不记块文本——避免把全文文档内容写入日志（api-data 规则第五节）。
    ★ 把**当时生效的可见范围**一并记入：stderr 字段 `scope_snapshot`（值 = scope_keys，
      沿用 V-1 的形状）；落表时写 {"scope_keys": [...], "kb_ids": [...]} 两轴都记
      ——kb 维度不折进 scope_keys（security.md 2.2：事后定位"当时他到底能看到什么"）。
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

    # ② query_logs 落表（T34）：只对带身份的线上请求生效。
    # 脚本/测试不传 user_id → 跳过（否则每次单测都会去连真实数据库）。
    if user_id is None:
        return
    try:
        result.query_log_id = query_log_repo.insert_log(
            user_id=user_id,
            # kb_id 只在"恰好一个知识库"时有意义；多库时完整列表进 scope_snapshot
            kb_id=kb_ids[0] if kb_ids and len(kb_ids) == 1 else None,
            question=question,
            scope_snapshot={
                "scope_keys": list(scope_keys),
                "kb_ids": list(kb_ids or []),
            },
            retrieved_chunk_ids=retrieved_chunk_ids,
            answer=result.answer,
            refused=result.refused,
            citations=[asdict(c) for c in result.citations],
            latency_ms=result.latency_ms,
            retrieve_ms=result.retrieve_ms,
            generate_ms=result.generate_ms,
            cost_cny=result.cost_cny,
            model_used=result.model_used,
        )
    except Exception as exc:  # noqa: BLE001 — 旁路记录失败必须兜住，见 docstring
        # R7：不静默——打 stderr 让人看得见；但不阻断问答返回
        print(
            f"⚠️ query_logs 落表失败（问答结果不受影响）：{exc!r}",
            file=sys.stderr,
        )
