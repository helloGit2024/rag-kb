"""答案生成器（V-1）：提示词拼装 → 调 LLM 网关 → 引用绑定 → 拒答判定。

职责边界：
  - 提示词本体在 app/prompts/answer_v1.md，本文件只做占位符替换（R4）；
  - 模型调用一律经 app/core/llm_gateway.chat()（R11），不直连模型接口；
  - 拒答句式固定且唯一（R5），本模块在加载模板时校验，防提示词漂移。

上游：app/repos/chunk_repo.search_chunks() 返回的 ChunkHit 列表。
下游：T08 的 API 层把 GeneratedAnswer 映射成 openapi.yaml 的 QueryResult。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.llm_gateway import chat
from app.repos.chunk_repo import ChunkHit

# R5：拒答必须且仅输出这一句，结构化字段置 refused=true
REFUSAL_TEXT = "根据现有资料，我无法回答这个问题。"

_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "answer_v1.md"
_CITATION_RE = re.compile(r"\[(\d+)\]")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")  # 金额/日期/数量中的数字串
_SNIPPET_MAX = 200


@dataclass
class Citation:
    """一条引用，字段对齐 openapi.yaml 的 Citation（T08 直接映射）。"""

    index: int
    doc_id: int
    doc_title: str
    chunk_id: int
    char_start: int
    char_end: int
    snippet: str
    score: float


@dataclass
class GeneratedAnswer:
    """一次生成的完整结果（记账字段供 T32 延迟与成本埋点）。"""

    answer: str
    refused: bool
    citations: list[Citation]
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


def load_prompt_template(path: Path | None = None) -> str:
    """读取提示词模板并校验固定拒答句式仍在（R5 漂移防护）。"""
    p = path or _PROMPT_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"提示词模板不存在：{p}（R4：提示词必须独立成文件，不得写死在 .py 里）"
        )
    tpl = p.read_text(encoding="utf-8")
    if REFUSAL_TEXT not in tpl:
        raise ValueError(
            f"提示词模板 {p} 缺少固定拒答句式，拒绝加载（R5）：{REFUSAL_TEXT}"
        )
    return tpl


def generate_answer(
    question: str,
    hits: list[ChunkHit],
    *,
    transport: httpx.BaseTransport | None = None,
) -> GeneratedAnswer:
    """依据检索结果生成答案。

    Args:
        question: 用户问题。
        hits: 检索命中的块（按相关度降序），编号即答案里的 [1][2]…。
        transport: 仅测试注入 httpx.MockTransport 用，业务代码不要传。

    Returns:
        GeneratedAnswer。hits 为空时不调用模型，直接返回固定拒答句。

    Raises:
        ValueError: question 为空（R7 显式失败）。
        GatewayError: 模型调用失败，原样上抛由上层转结构化错误码（R7）。
    """
    if not question.strip():
        raise ValueError("question 不能为空（R7）")

    if not hits:
        # 无候选资料，模型必然拒答——省掉这次注定无意义的调用
        return GeneratedAnswer(
            answer=REFUSAL_TEXT,
            refused=True,
            citations=[],
            prompt_tokens=0,
            completion_tokens=0,
            latency_ms=0,
        )

    prompt = _render_prompt(question, hits)
    result = chat([{"role": "user", "content": prompt}], transport=transport)
    raw = result.content.strip()

    if _is_refusal(raw):
        if raw != REFUSAL_TEXT:
            # 模型加了前后缀：纠偏为固定句式（可见的告警，不静默吞掉）
            print(
                f"⚠️ 模型输出包含拒答句式以外内容，已归一为固定句式（R5）：{raw[:120]!r}"
            )
        return GeneratedAnswer(
            answer=REFUSAL_TEXT,
            refused=True,
            citations=[],
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
        )

    citations = _bind_citations(raw, hits)
    if not citations:
        print("⚠️ 答案未包含有效引用编号 [n]，无法回绑出处（可能违反了提示词规则 2）")

    # R6 + rag-core「生成」规则：答案里的数字必须在被引原文中逐字出现，
    # 否则按可疑处理并拒答——改写数字比答不出来严重得多。
    # （严重度已由项目所有者确认：V-1 即按拒答处理，不降为仅告警。）
    bad = _unverifiable_numbers(raw, citations, hits)
    if bad:
        print(f"⚠️ 答案含引用原文中找不到的数字 {bad}，按可疑处理并拒答（R6）")
        return GeneratedAnswer(
            answer=REFUSAL_TEXT,
            refused=True,
            citations=[],
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
        )

    return GeneratedAnswer(
        answer=raw,
        refused=False,
        citations=citations,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=result.latency_ms,
    )


def _render_prompt(question: str, hits: list[ChunkHit]) -> str:
    """把资料与问题填进模板。用 str.replace 而非 format——问题文本可能含花括号。"""
    tpl = load_prompt_template()
    return tpl.replace("{{context}}", _build_context(hits)).replace(
        "{{question}}", question.strip()
    )


def _build_context(hits: list[ChunkHit]) -> str:
    """把命中块编成带编号的资料，编号 [1]..[k] 与 citations.index 严格对应。"""
    blocks = []
    for i, h in enumerate(hits, start=1):
        blocks.append(f"[{i}]（来源：《{h.doc_title}》）\n{h.content}")
    return "\n\n".join(blocks)


def _bind_citations(answer: str, hits: list[ChunkHit]) -> list[Citation]:
    """把答案里的 [n] 映射回真实 chunk。按首次出现顺序；越界编号忽略并告警。"""
    bound: dict[int, Citation] = {}
    for m in _CITATION_RE.finditer(answer):
        n = int(m.group(1))
        if n in bound:
            continue
        if n < 1 or n > len(hits):
            print(f"⚠️ 答案引用了不存在的编号 [{n}]（资料只有 1..{len(hits)}），已忽略")
            continue
        h = hits[n - 1]
        bound[n] = Citation(
            index=n,
            doc_id=h.doc_id,
            doc_title=h.doc_title,
            chunk_id=h.chunk_id,
            char_start=h.char_start,
            char_end=h.char_end,
            snippet=h.content[:_SNIPPET_MAX],
            score=h.score,
        )
    return list(bound.values())


def _is_refusal(text: str) -> bool:
    """判为拒答。固定句式出现即算——R5 要求拒答只能输出这一句。"""
    return REFUSAL_TEXT in text


def _unverifiable_numbers(
    answer: str, citations: list[Citation], hits: list[ChunkHit]
) -> list[str]:
    """返回答案里「在被引原文中逐字找不到」的数字串（R6）。

    只取数字串本身比对（如答案写"10 天"、原文写"10天"仍算通过），
    这样既能抓住"1204 万改成约 1200 万"这类改写，
    又不会因为空格/标点的排版差异误判。
    """
    cited_text = "\n".join(hits[c.index - 1].content for c in citations)
    source = cited_text if citations else "\n".join(h.content for h in hits)
    # 先剔除 [1][2] 这类引用编号，它们不是答案内容里的数字
    body = _CITATION_RE.sub("", answer)
    missing = []
    for token in dict.fromkeys(_NUMBER_RE.findall(body)):
        if token not in source:
            missing.append(token)
    return missing
