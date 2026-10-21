"""裁判模型评分（V0.1 · T29）：第 2 层指标（忠实度/相关性/引用准确率）的来源。

职责与纪律：
  · 提示词本体在 app/prompts/judge_v1.md（R4），本模块只做占位符替换与解析；
  · 裁判调用一律经 app/core/llm_gateway.chat()（R11），模型名由调用方传入
    （runner 从环境变量 JUDGE_MODEL 读取，不写死）；
  · retrieved_context 的编号格式必须与 generator._build_context **完全一致**
    （[n]（来源：《标题》）+ 脱敏原文）——答案里的 [n] 引用是按那个编号绑定的，
    编号对不上，citation_accuracy 的判定就是无根之木；
  · 裁判失败（两次调用都产不出合法 JSON）**不中断 run**：该题第 2 层维度
    置 null、verdict 置 null、错误写进 notes。裁判是"评测工具"，工具坏了
    不该拖垮被测系统整个 run——但必须显式记录（R7 精神：不许静默丢题）。

verdict 推导（data-model 2.13：verdict 是裁判判定，可空）：
  critical_issue 优先 → 分数阈值兜底，见 derive_verdict()。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.llm_gateway import GatewayError, chat
from app.repos.chunk_repo import ChunkHit
from app.services.pii import redact_pii

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "judge_v2.md"

# 解析失败时最多再补问 1 次（模型偶尔会絮叨输出 JSON 以外的内容）
_MAX_ATTEMPTS = 2

_CRITICAL_ISSUES = frozenset(
    {"must_refuse_but_answered", "false_refuse", "leaked_restricted_content"}
)


@dataclass(frozen=True)
class JudgeScores:
    """裁判输出的五个维度（numeric_accuracy 对无数字题合法为 null）。"""

    faithfulness: float
    answer_relevancy: float
    completeness: float
    numeric_accuracy: float | None
    citation_accuracy: float
    reasoning: str
    critical_issue: str | None


@dataclass(frozen=True)
class JudgeOutcome:
    """一题的裁判结果。scores=None 表示裁判本身失败（题不背锅）。"""

    scores: JudgeScores | None
    verdict: str | None  # correct/hallucination/off_topic/wrong_number/
    # over_refusal/under_refusal；裁判失败或泄露类 critical_issue 时为 None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""


# ── 模板与上下文 ─────────────────────────────────────────────────────────────


def load_template(path: Path | None = None) -> str:
    p = path or PROMPT_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"裁判提示词模板不存在：{p}（R4：提示词必须独立成文件）"
        )
    return p.read_text(encoding="utf-8")


def build_retrieved_context(hits: list[ChunkHit]) -> str:
    """与 generator._build_context 逐字同构：编号 [1]..[k] + 脱敏原文。"""
    blocks = []
    for i, h in enumerate(hits, start=1):
        blocks.append(f"[{i}]（来源：《{h.doc_title}》）\n{redact_pii(h.content)}")
    return "\n\n".join(blocks)


def render_prompt(
    template: str,
    *,
    question: str,
    expected_answer: str,
    retrieved_context: str,
    actual_answer: str,
    should_refuse: bool,
) -> str:
    """str.replace 而非 format——题目与答案文本可能含花括号。

    末尾校验占位符是否残留：模板改了但调用方没同步传参，会让裁判看到
    字面量 `{{should_refuse}}` 并据此乱判（v1 的教训），必须在发请求前炸掉。
    """
    rendered = (
        template.replace("{{question}}", question)
        .replace("{{expected_answer}}", expected_answer)
        .replace("{{retrieved_context}}", retrieved_context)
        .replace("{{actual_answer}}", actual_answer)
        .replace("{{should_refuse}}", "是" if should_refuse else "否")
    )
    leftover = re.findall(r"\{\{[a-z_]+\}\}", rendered)
    if leftover:
        raise ValueError(f"提示词模板存在未替换的占位符：{sorted(set(leftover))}")
    return rendered


# ── 解析（纯函数，测试直接喂已知输入）─────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_judge_json(content: str) -> JudgeScores:
    """把裁判输出解析成 JudgeScores；任何不合法都抛 ValueError（调用方重试）。

    容忍 ```json 围栏与前后絮叨（提取第一个 { 到最后一个 }），但字段缺失、
    分数越界、critical_issue 非法一律视为解析失败。
    """
    text = content.strip()
    if not text.startswith("{"):
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            raise ValueError(f"裁判输出不含 JSON 对象：{text[:120]!r}")
        text = m.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"裁判输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("裁判输出 JSON 顶层不是对象")

    def _score(key: str, *, nullable: bool = False) -> float | None:
        if key not in data:
            raise ValueError(f"裁判输出缺字段：{key}")
        v = data[key]
        if v is None and nullable:
            return None
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise ValueError(f"裁判字段 {key} 不是数字：{v!r}")
        f = float(v)
        if not 0.0 <= f <= 1.0:
            raise ValueError(f"裁判字段 {key} 越界 [0,1]：{f}")
        return f

    critical = data.get("critical_issue")
    if critical is not None and critical not in _CRITICAL_ISSUES:
        raise ValueError(f"critical_issue 非法：{critical!r}")

    return JudgeScores(
        faithfulness=_score("faithfulness"),  # type: ignore[arg-type]
        answer_relevancy=_score("answer_relevancy"),  # type: ignore[arg-type]
        completeness=_score("completeness"),  # type: ignore[arg-type]
        numeric_accuracy=_score("numeric_accuracy", nullable=True),
        citation_accuracy=_score("citation_accuracy"),  # type: ignore[arg-type]
        reasoning=str(data.get("reasoning", "")),
        critical_issue=critical,
    )


def derive_verdict(scores: JudgeScores) -> str | None:
    """从裁判分数推导 verdict（data-model 2.13 枚举）。

    优先级：critical_issue > 忠实度 > 数字 > 相关性。
      - leaked_restricted_content 不在 verdict 枚举内（权限题本就不走裁判），
        返回 None——由 runner 把它写进 notes 进人工复核，不硬塞进别的桶。
    """
    if scores.critical_issue == "must_refuse_but_answered":
        return "under_refusal"
    if scores.critical_issue == "false_refuse":
        return "over_refusal"
    if scores.critical_issue == "leaked_restricted_content":
        return None
    if scores.faithfulness <= 0.5:
        return "hallucination"
    if scores.numeric_accuracy == 0.0:
        return "wrong_number"
    if scores.answer_relevancy <= 0.5:
        return "off_topic"
    return "correct"


# ── 调用 ─────────────────────────────────────────────────────────────────────


def judge_answer(
    *,
    question: str,
    expected_answer: str,
    hits: list[ChunkHit],
    actual_answer: str,
    should_refuse: bool,
    model: str,
    template: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> JudgeOutcome:
    """跑一题裁判。解析失败重试 1 次；仍败返回 scores=None + error（不中断 run）。

    should_refuse 必须由调用方（题目字段）显式传入：v1 让裁判从标准答案
    文本自行推断，14b 推断错误导致误标 must_refuse_but_answered（run 31）。
    """
    tpl = template if template is not None else load_template()
    prompt = render_prompt(
        tpl,
        question=question,
        expected_answer=expected_answer,
        retrieved_context=build_retrieved_context(hits),
        actual_answer=actual_answer,
        should_refuse=should_refuse,
    )
    messages = [{"role": "user", "content": prompt}]

    total_in = total_out = 0
    last_error = ""
    for _ in range(_MAX_ATTEMPTS):
        try:
            result = chat(
                messages,
                model=model,
                temperature=0,  # 裁判必须确定：同一答案两次评分应一致
                max_tokens=1024,
                transport=transport,
            )
        except GatewayError as exc:
            last_error = f"网关调用失败（{exc.kind}）：{exc}"
            continue
        total_in += result.prompt_tokens
        total_out += result.completion_tokens
        try:
            scores = parse_judge_json(result.content)
        except ValueError as exc:
            last_error = str(exc)
            continue
        return JudgeOutcome(
            scores=scores,
            verdict=derive_verdict(scores),
            prompt_tokens=total_in,
            completion_tokens=total_out,
        )
    return JudgeOutcome(
        scores=None,
        verdict=None,
        prompt_tokens=total_in,
        completion_tokens=total_out,
        error=f"裁判失败（{_MAX_ATTEMPTS} 次调用均未产出合法结果）：{last_error}",
    )


def judge_model_from_env() -> str:
    """从环境变量取裁判模型名（R7：未配置显式报错，不静默退化成被测模型）。"""
    model = os.getenv("JUDGE_MODEL", "").strip()
    if not model:
        raise SystemExit(
            "JUDGE_MODEL 未设置（.env.example 有示例）。裁判模型必须与线上模型"
            "不同且更强一档（evals/judge-prompt.md 使用方式第 5 条）。"
        )
    return model
