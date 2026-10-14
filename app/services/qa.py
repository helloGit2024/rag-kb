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
from dataclasses import dataclass

from app.core.embedding import embed_texts
from app.repos.chunk_repo import search_chunks
from app.services.generator import Citation, generate_answer


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
    """把问题跑完整条链路。

    Args:
        question: 用户问题（调用方应已校验非空/未超长）。
        top_k: 检索条数。
        scope_keys: 用户可见范围。★ 必须显式传入，权限过滤在 SQL 内完成（R2）。

    Raises:
        ValueError: question 为空或 scope_keys 为空（R7 显式失败）。
        EmbeddingError / GatewayError: 模型调用失败，原样上抛由 API 层转结构化错误码。
    """
    if not question.strip():
        raise ValueError("question 不能为空（R7）")
    if not scope_keys:
        # R7：宁可显式失败，也不静默检索出越权内容
        raise ValueError("scope_keys 不能为空（R2/R7）")

    t0 = time.perf_counter()
    query_vec = embed_texts([question])[0]
    hits = search_chunks(query_vec, scope_keys, top_k=top_k)
    t1 = time.perf_counter()
    gen = generate_answer(question, hits)
    t2 = time.perf_counter()

    retrieve_ms = round((t1 - t0) * 1000)
    generate_ms = round((t2 - t1) * 1000)

    _log_qa(question, hits, gen, retrieve_ms, generate_ms, scope_keys)

    return QaResult(
        answer=gen.answer,
        refused=gen.refused,
        citations=gen.citations,
        retrieve_ms=retrieve_ms,
        generate_ms=generate_ms,
        latency_ms=retrieve_ms + generate_ms,
    )


def _log_qa(
    question: str,
    hits: list,
    gen,
    retrieve_ms: int,
    generate_ms: int,
    scope_keys: list[str],
) -> None:
    """落一行结构化问答记录。

    rag-core「生成」规则要求每条问答都有结构化记录；V-1 还没有 query_logs 表
    （T02 只建了 documents/chunks），因此先打印一行 JSON，落表留到 V0.1 T32/T33。
    只记 chunk_id 不记块文本——避免把全文文档内容写入日志（api-data 规则第五节）。
    ★ 把**当时生效的可见范围**一并记入，字段名固定为 `scope_snapshot`（值 = 参数
      scope_keys）：security.md 2.2 要求每次问答都能事后定位"当时他到底能看到什么"。
      与 `query_logs.scope_snapshot`（T34 落表）共用同一个名字，落表时无需再做名字映射。
      四个易混词的区分见 docs/glossary.md 第七节 —— scope_key（单个标签）/
      scope_keys（内存里的值）/ scope_snapshot（落盘字段名）/
      缓存指纹（hash，局部变量）。
    """
    print(
        json.dumps(
            {
                "event": "qa",
                "question": question,
                "scope_snapshot": list(scope_keys),
                "retrieved_chunk_ids": [h.chunk_id for h in hits],
                "cited_chunk_ids": [c.chunk_id for c in gen.citations],
                "refused": gen.refused,
                "degraded": False,
                "retrieve_ms": retrieve_ms,
                "generate_ms": generate_ms,
                "latency_ms": retrieve_ms + generate_ms,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
