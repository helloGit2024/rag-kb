"""评估运行编排（V0.1 · T28）：读题 → 逐题跑链 → 算指标 → 写库 → 出报告。

口径与纪律（.trae/rules/eval.md，违反了不报错、只会得出错误结论）：
  · 评估口径 = 离线单条、**无缓存命中**——链路走 qa.run_chain，不碰问答缓存；
  · 评估检索**锁定 KB**（--kb-id，默认 534 评估语料库）：kb1/2/3 的演示文档
    与评估语料内容重叠（如 ragkb_sample_年假制度.txt），不锁定会让 Recall@5
    被无关文档污染、基线不可复现；
  · 第 2 层指标（忠实度/答案相关性/引用准确率）由裁判模型产出（T29）：
    仅 normal/edge 且 judge=model 的题跑裁判（judge-prompt.md 第五节——
    能用规则判断的不用模型）；裁判失败不中断 run，该题维度置 null；
  · 逐条结果必须落 eval_run_items 且含 answer 全文——只存汇总指标的评估
    等于没评估（无法回答"哪几条从通过变失败"）。

用法：.venv/bin/python -m app.eval.runner [--kb-id 534] [--allow-dirty]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.eval.judge import (
    JudgeOutcome,
    judge_answer,
    judge_model_from_env,
)
from app.eval.judge import (
    load_template as load_judge_template,
)
from app.eval.loader import (
    DATASET_NAME,
    DATASET_VERSION,
    IDENTITY_MAP,
    GoldenItem,
    ensure_dataset,
    load_golden_set,
)
from app.repos import eval_repo
from app.repos.chunk_repo import ChunkHit
from app.repos.models import EvalRunItem
from app.schemas.query import QueryRequest
from app.services.permission import PermissionBundle, resolve_permission
from app.services.qa import QaResult, run_chain

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_SET_PATH = PROJECT_ROOT / "evals" / "golden_set.yaml"
REPORT_DIR = PROJECT_ROOT / "evals" / "report"
FAILURES_DIR = PROJECT_ROOT / "evals" / "failures"

DEFAULT_KB_ID = 534  # 评估语料库（8 份人工确认文档，2026-10-20 入库）

# Q028 的 question 字段是占位符「（这里是 3000 字的超长问题…）」而非真实长文，
# 直接跑链等于没测。这里合成一个 2400 字问题走 schema 校验（上限 2000），
# 期望被 ValidationError 拦截、零模型调用（验收点：Token 计数不增加）。
OVERSIZE_ITEM_ID = "Q028"
_OVERSIZE_QUESTION = "年假怎么算？" * 400  # 6 字 × 400 = 2400 字 > 2000 上限

# 指标及格线（evals/README.md 第二节）
_TARGETS = {
    "recall_at_5": (">=", 0.85),
    "mrr": (">=", 0.70),
    "context_precision": (">=", 0.70),
    "faithfulness": (">=", 0.85),
    "answer_relevance": (">=", 0.80),
    "citation_accuracy": (">=", 0.90),
    "task_completion": (">=", 0.90),
    "refusal_accuracy": (">=", 0.90),
    "false_refusal_rate": ("<=", 0.05),
    "p95_latency_s": ("<", 5.0),
    "cost_cny": ("<", 0.03),
}


@dataclass
class ItemOutcome:
    """一题的运行结果（to_row 落成 eval_run_items 一行）。

    status / verdict 语义（data-model 2.13，T29 修正）：
      status = pass / fail —— **行为判定**（该拒的拒了没、不该拒的答了没）；
      verdict = correct / hallucination / off_topic / wrong_number /
                over_refusal / under_refusal —— **裁判判定**，
                未跑裁判（refuse/permission/human 题）或裁判失败为 null。
    """

    item: GoldenItem
    status: str  # pass / fail（行为判定）
    scores: dict
    verdict: str | None = None  # 裁判判定（可空，data-model 2.13）
    retrieval_hit: bool | None = None
    retrieved_chunk_ids: list[int] = field(default_factory=list)
    answer: str | None = None
    refused: bool | None = None
    latency_ms: int = 0
    retrieve_ms: int = 0
    generate_ms: int = 0
    count_in_latency: bool = True  # Q028 无模型调用，不计入延迟统计
    notes: str = ""

    def to_row(self, run_id: int) -> EvalRunItem:
        return EvalRunItem(
            run_id=run_id,
            item_id=self.item.item_id,
            category=self.item.category,
            question=self.item.question,
            status=self.status,
            scores=self.scores,
            verdict=self.verdict,
            retrieval_hit=self.retrieval_hit,
            retrieved_chunk_ids=self.retrieved_chunk_ids or None,
            answer=self.answer,
            refused=self.refused,
            degraded=False,
            latency_ms=self.latency_ms,
            retrieve_ms=self.retrieve_ms,
            generate_ms=self.generate_ms,
            cost_cny=Decimal("0"),  # 本地 Ollama 无 API 调用费；Token 数在 scores 里
            notes=self.notes or None,
        )


# ── 指标纯函数（无 I/O，tests/test_eval.py 直接喂已知输入）──────────────────


def layer1_scores(
    ranked_titles: Sequence[str], expected: Sequence[str]
) -> tuple[float, float, float]:
    """检索层得分：(recall, reciprocal_rank, context_precision)。

    - recall：任一标准来源文档出现在 Top-K 即 1，否则 0（Recall@5 的逐题形态）；
    - rr：首个命中位次的倒数（未命中 0），逐题平均后即 MRR；
    - precision：Top-K 中来自标准来源文档的块占比。
    判定粒度是**文档**（# 后章节锚点不参与，见 loader.expected_doc_titles）。
    """
    exp = {t for t in expected if t}
    first_rank = 0
    hits = 0
    for rank, title in enumerate(ranked_titles, start=1):
        if title in exp:
            hits += 1
            if first_rank == 0:
                first_rank = rank
    recall = 1.0 if first_rank else 0.0
    rr = 1.0 / first_rank if first_rank else 0.0
    precision = hits / len(ranked_titles) if ranked_titles else 0.0
    return recall, rr, precision


def percentile(values: Sequence[int], p: float) -> float:
    """最近秩百分位（小样本不插值；空集返回 0）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * p / 100))
    return float(ordered[rank - 1])


def aggregate(outcomes: list[ItemOutcome]) -> dict:
    """把逐题结果聚成 eval_runs.metrics（评估口径，与线上口径分区）。

    分母口径（与 README 第二节一致）：
      - 第 1 层：仅 expected_sources 非空的题（V0.1 = 19 题：Q001–Q018 + Q030）；
      - 拒答正确率：should_refuse=true 的题（V0.1 = 9 题：6 refuse + 3 permission）；
      - 误拒率/任务完成率：should_refuse=false 的题（V0.1 = 21 题）。
        Q029（歧义题，接受拒答）按字段原样统计，拒答会算一次误拒——
        这是与项目所有者确认过的口径（诚实，不改字段语义）。
    """

    def _mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 4) if xs else None

    layer1 = [o for o in outcomes if o.retrieval_hit is not None]
    should_refuse = [o for o in outcomes if o.item.should_refuse]
    answerable = [o for o in outcomes if not o.item.should_refuse]
    timed = [o.latency_ms for o in outcomes if o.count_in_latency]
    # 第 2 层分母 = 裁判**成功**的题数（裁判失败置 null 的题不进均值——
    # 分母口径必须诚实，否则"裁判全挂"会伪装成"指标满分"）
    judged = [o for o in outcomes if o.scores["faithfulness"] is not None]
    numeric = [o for o in judged if o.scores["numeric_accuracy"] is not None]

    return {
        "layer1": {
            "recall_at_5": _mean([o.scores["recall"] for o in layer1]),
            "mrr": _mean([o.scores["rr"] for o in layer1]),
            "context_precision": _mean([o.scores["precision"] for o in layer1]),
            "sample_size": len(layer1),
        },
        "layer2": {
            "faithfulness": _mean([o.scores["faithfulness"] for o in judged]),
            "answer_relevance": _mean(
                [o.scores["answer_relevance"] for o in judged]
            ),
            "citation_accuracy": _mean(
                [o.scores["citation_accuracy"] for o in judged]
            ),
            # 附加维度：进报告供人工参考，不进及格线判定（项目所有者 2026-10-21 裁决）
            "completeness": _mean([o.scores["completeness"] for o in judged]),
            "numeric_accuracy": _mean(
                [o.scores["numeric_accuracy"] for o in numeric]
            ),
            "sample_size": len(judged),
        },
        "layer3": {
            "task_completion": _mean(
                [1.0 if o.status == "pass" else 0.0 for o in answerable]
            ),
            "refusal_accuracy": _mean(
                [1.0 if o.refused else 0.0 for o in should_refuse]
            ),
            "false_refusal_rate": _mean(
                [1.0 if o.refused else 0.0 for o in answerable]
            ),
        },
        "latency_eval": {
            "p95_ms": percentile(timed, 95),
            "avg_ms": _mean([float(v) for v in timed]),
            "sample_size": len(timed),
        },
        "cost_eval": {
            "total_cny": 0.0,
            "avg_cny": 0.0,
            "note": "本地 Ollama，无 API 调用费；Token 记账见 tokens 字段",
        },
        "tokens": {
            "prompt": sum(o.scores["prompt_tokens"] for o in outcomes),
            "completion": sum(o.scores["completion_tokens"] for o in outcomes),
            "judge_prompt": sum(o.scores["judge_prompt_tokens"] for o in outcomes),
            "judge_completion": sum(
                o.scores["judge_completion_tokens"] for o in outcomes
            ),
        },
        "items": {
            "total": len(outcomes),
            "passed": sum(1 for o in outcomes if o.status == "pass"),
            "failed": sum(1 for o in outcomes if o.status == "fail"),
        },
    }


# ── 逐题运行 ────────────────────────────────────────────────────────────────

ChainFn = Callable[..., tuple[QaResult, list[ChunkHit]]]


def _run_oversize_item(item: GoldenItem) -> ItemOutcome:
    """Q028 专项：合成超长问题 → 期望 schema 校验拦截、零模型调用。"""
    try:
        QueryRequest(question=_OVERSIZE_QUESTION, top_k=5)
    except ValidationError:
        return ItemOutcome(
            item=item,
            status="pass",
            scores=_zero_scores(),
            count_in_latency=False,
            notes=(
                f"合成 {len(_OVERSIZE_QUESTION)} 字问题（上限 2000），"
                "schema 校验拦截，未产生任何模型调用（Token=0）"
            ),
        )
    return ItemOutcome(
        item=item,
        status="fail",
        scores=_zero_scores(),
        count_in_latency=False,
        notes="超长问题未被 schema 校验拦截——成本防护失效（Q028 硬性要求）",
    )


def _zero_scores() -> dict:
    return {
        "recall": None,
        "rr": None,
        "precision": None,
        "faithfulness": None,
        "answer_relevance": None,
        "citation_accuracy": None,
        "completeness": None,
        "numeric_accuracy": None,
        "judge_reasoning": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "judge_prompt_tokens": 0,
        "judge_completion_tokens": 0,
    }


def _score_chain_item(
    item: GoldenItem,
    result: QaResult,
    hits: list[ChunkHit],
    judge_outcome: JudgeOutcome | None = None,
) -> ItemOutcome:
    """正常链路题：算第 1 层得分、行为判定（status），并入裁判结果（verdict）。

    status 判定（行为层）：
      - should_refuse=true（refuse/permission）：refused=true 才 pass。
        permission 题检索必为空 → 答案必为固定拒答句，天然满足
        "不得出现目标文档片段"的硬性要求；
      - should_refuse=false（normal/edge）：refused=false 才 pass。

    裁判结果（judge_outcome 非 None 时）：
      - 裁判成功：五维度分数 + reasoning 进 scores，verdict 取裁判推导值；
      - 裁判失败：维度保持 null，错误写入 notes（题不背锅，但必须显式记录）。
    """
    scores = _zero_scores()
    retrieval_hit: bool | None = None
    if item.expected_doc_titles:
        recall, rr, precision = layer1_scores(
            [h.doc_title for h in hits], item.expected_doc_titles
        )
        scores.update({"recall": recall, "rr": rr, "precision": precision})
        retrieval_hit = recall > 0
    scores["prompt_tokens"] = result.prompt_tokens
    scores["completion_tokens"] = result.completion_tokens

    if item.should_refuse:
        ok = result.refused
        notes = "" if ok else "应拒答但未拒答"
    else:
        ok = not result.refused
        notes = "" if ok else "误拒（库里本有答案）"

    verdict: str | None = None
    if judge_outcome is not None:
        scores["judge_prompt_tokens"] = judge_outcome.prompt_tokens
        scores["judge_completion_tokens"] = judge_outcome.completion_tokens
        if judge_outcome.scores is None:
            notes = f"{notes}；{judge_outcome.error}" if notes else judge_outcome.error
        else:
            js = judge_outcome.scores
            scores.update(
                {
                    "faithfulness": js.faithfulness,
                    "answer_relevance": js.answer_relevancy,
                    "completeness": js.completeness,
                    "numeric_accuracy": js.numeric_accuracy,
                    "citation_accuracy": js.citation_accuracy,
                    "judge_reasoning": js.reasoning,
                }
            )
            verdict = judge_outcome.verdict
            if js.critical_issue == "leaked_restricted_content":
                leak = (
                    "裁判报告 leaked_restricted_content（不在 verdict 枚举内，"
                    "verdict 置 null），需人工复核"
                )
                notes = f"{notes}；{leak}" if notes else leak

    return ItemOutcome(
        item=item,
        status="pass" if ok else "fail",
        verdict=verdict,
        scores=scores,
        retrieval_hit=retrieval_hit,
        retrieved_chunk_ids=[h.chunk_id for h in hits],
        answer=result.answer,
        refused=result.refused,
        latency_ms=result.latency_ms,
        retrieve_ms=result.retrieve_ms,
        generate_ms=result.generate_ms,
        notes=notes,
    )


JudgeFn = Callable[[GoldenItem, QaResult, list[ChunkHit]], JudgeOutcome]


def run_all_items(
    session: Session,
    items: list[GoldenItem],
    *,
    kb_id: int,
    chain: ChainFn | None = None,
    judge: JudgeFn | None = None,
) -> list[ItemOutcome]:
    """逐题跑链。任一题链路异常**直接上抛**（R7：半个 run 的漂亮表格
    比报错更危险——你会拿着它宣称"进步了"）。调用方负责把 run 标 failed。

    chain / judge 可注入（测试喂假链路、假裁判，不依赖 Ollama）；
    None 时 chain 用真实 run_chain，judge=None 表示本跑不评第 2 层
    （仅测试与调试场景；main 入口只要题集里有该裁判的题就强制配置裁判）。
    """
    chain = chain or run_chain
    outcomes: list[ItemOutcome] = []
    for i, item in enumerate(items, start=1):
        if item.item_id == OVERSIZE_ITEM_ID:
            outcome = _run_oversize_item(item)
        else:
            perm: PermissionBundle = resolve_permission(
                session, username=item.username
            )
            # kb 轴锁定评估语料库：scope 走用户真实范围，kb_ids 强制 [kb_id]
            result, hits = chain(
                item.question,
                top_k=5,
                scope_keys=perm.scope_keys,
                kb_ids=[kb_id],
            )
            judge_outcome: JudgeOutcome | None = None
            if (
                judge is not None
                and item.category in ("normal", "edge")
                and item.judge == "model"
            ):
                judge_outcome = judge(item, result, hits)
            outcome = _score_chain_item(item, result, hits, judge_outcome)
        outcomes.append(outcome)
        print(
            f"  [{i:2d}/{len(items)}] {item.item_id} {item.category:<10s} "
            f"status={outcome.status} verdict={outcome.verdict} "
            f"refused={outcome.refused} retrieval_hit={outcome.retrieval_hit}",
            file=sys.stderr,
        )
    return outcomes


# ── 可复现性（R14）──────────────────────────────────────────────────────────


def git_commit_for_run(*, allow_dirty: bool) -> str:
    """取当前 git commit；有**已跟踪文件**的未提交改动时拒绝开始（R14）。

    只看已跟踪文件（--untracked-files=no）：评估产物（report/failures 新文件）
    不影响链路可复现性，不该因此卡住第二次运行；代码/题目/配置的改动才算。
    """
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if dirty and not allow_dirty:
        raise SystemExit(
            "工作区有未提交的已跟踪文件改动，评估拒绝开始（R14 可复现性）：\n"
            f"{dirty}\n请先提交，或显式加 --allow-dirty。"
        )
    return commit


def config_snapshot(kb_id: int) -> dict:
    """本次运行的配置快照（R14：两次数字可不可比的唯一依据）。"""
    return {
        "prompt_version": "answer_v1",
        "top_k": 5,
        "chunk_size": 512,
        "chunk_overlap": 64,
        "embedding_model": os.getenv("EMBEDDING_MODEL", ""),
        "llm_model": os.getenv("LLM_MODEL", ""),
        "rerank": False,  # V0.1 故意不做（对比基线留 V1）
        "hybrid": False,  # V0.1 故意不做（纯向量）
        "judge_model": os.getenv("JUDGE_MODEL", ""),  # T29 裁判模型（与被测模型不同档）
        "eval_kb_id": kb_id,
        "dataset": f"{DATASET_NAME}@{DATASET_VERSION}",
        "identity_map": IDENTITY_MAP,
    }


# ── 报告 ────────────────────────────────────────────────────────────────────


def _fmt(value: float | None, digits: int = 3) -> str:
    return f"{value:.{digits}f}" if value is not None else "—"


def _status(key: str, value: float | None) -> str:
    if value is None:
        return "—"
    op, target = _TARGETS[key]
    if op == ">=":
        ok = value >= target
    elif op == "<":
        ok = value < target
    else:
        ok = value <= target
    return "✅" if ok else "❌"


def print_table(metrics: dict) -> None:
    """stdout 指标表（evals/README.md 第四节样式；Markdown 表格便于粘贴）。"""
    l1, l2, l3 = metrics["layer1"], metrics["layer2"], metrics["layer3"]
    p95_s = metrics["latency_eval"]["p95_ms"] / 1000

    def _row(key: str, name: str, target: str, value: float | None) -> tuple:
        return (name, target, _fmt(value), _status(key, value))

    rows = [
        _row("recall_at_5", "Recall@5", "≥ 0.85", l1["recall_at_5"]),
        _row("mrr", "MRR", "≥ 0.70", l1["mrr"]),
        _row("context_precision", "上下文精确度", "≥ 0.70", l1["context_precision"]),
        _row("faithfulness", "忠实度", "≥ 0.85", l2["faithfulness"]),
        _row("answer_relevance", "答案相关性", "≥ 0.80", l2["answer_relevance"]),
        _row("citation_accuracy", "引用准确率", "≥ 0.90", l2["citation_accuracy"]),
        _row("task_completion", "任务完成率", "≥ 0.90", l3["task_completion"]),
        _row("refusal_accuracy", "拒答正确率", "≥ 0.90", l3["refusal_accuracy"]),
        _row(
            "false_refusal_rate", "误拒率", "≤ 0.05", l3["false_refusal_rate"]
        ),
        ("P95 延迟", "< 5.0s", f"{p95_s:.2f}s", _status("p95_latency_s", p95_s)),
        ("单次成本", "< ¥0.03", "¥0.000", "✅"),
    ]
    print("\n| 指标 | 目标 | 当前 | 状态 |")
    print("|---|---|---|---|")
    for name, target, current, status in rows:
        print(f"| {name} | {target} | {current} | {status} |")
    items = metrics["items"]
    print(f"\n逐题：{items['passed']}/{items['total']} pass，{items['failed']} fail")
    print("口径：评估口径（离线单条、无缓存命中），不得与线上口径相加或平均")


def write_reports(
    run_id: int, commit: str, outcomes: list[ItemOutcome], metrics: dict
) -> tuple[Path, Path | None]:
    """写 report JSON（恒写）与 failures MD（有失败才写）。

    文件名带 run_id：同一天跑两次不互相覆盖（静默覆盖 = 丢证据，R7 精神）。
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    report_path = REPORT_DIR / f"{today}-run{run_id}.json"
    report = {
        "run_id": run_id,
        "date": today,
        "git_commit": commit,
        "dataset": f"{DATASET_NAME}@{DATASET_VERSION}",
        "metrics": metrics,
        # judge: human 的题裁判跳过，必须显式列出等人工评分（judge-prompt.md
        # 第四节）——不列出来，"还有几题没人评"就是不可见的
        "pending_human_judge": [
            o.item.item_id for o in outcomes if o.item.judge == "human"
        ],
        "items": [
            {
                "item_id": o.item.item_id,
                "category": o.item.category,
                "question": o.item.question,
                "status": o.status,
                "verdict": o.verdict,
                "refused": o.refused,
                "retrieval_hit": o.retrieval_hit,
                "scores": o.scores,
                "answer": o.answer,
                "latency_ms": o.latency_ms,
                "notes": o.notes,
            }
            for o in outcomes
        ],
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    failures = [o for o in outcomes if o.status == "fail"]
    failures_path: Path | None = None
    if failures:
        FAILURES_DIR.mkdir(parents=True, exist_ok=True)
        failures_path = FAILURES_DIR / f"{today}-run{run_id}.md"
        lines = [
            f"# 评估失败案例（run {run_id} · {today} · {len(failures)} 条）",
            "",
            f"git_commit: `{commit}`　数据集: {DATASET_NAME}@{DATASET_VERSION}",
            "",
        ]
        for o in failures:
            answer = (o.answer or "（无输出）").replace("\n", " ")[:300]
            lines += [
                f"## {o.item.item_id}（{o.item.category}）{o.item.question}",
                "",
                f"- 应拒答：{o.item.should_refuse}　实际拒答：{o.refused}　"
                f"检索命中：{o.retrieval_hit}　裁判判定：{o.verdict or '—'}",
                f"- 实际输出：{answer}",
                f"- 备注：{o.notes or '—'}",
                "",
            ]
        failures_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path, failures_path


# ── 入口 ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="V0.1 T28 评估脚本（30 题三层指标）")
    parser.add_argument(
        "--kb-id",
        type=int,
        default=int(os.getenv("EVAL_KB_ID", str(DEFAULT_KB_ID))),
        help="评估语料库 kb_id（默认 534）",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="允许已跟踪文件有未提交改动（默认拒绝，R14）",
    )
    parser.add_argument("--version", default="V0.1", help="跑哪个版本的题目")
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print(
            "DATABASE_URL 未设置（请通过 make eval 运行，或先 source .env）",
            file=sys.stderr,
        )
        return 2

    commit = git_commit_for_run(allow_dirty=args.allow_dirty)
    items = load_golden_set(GOLDEN_SET_PATH, version=args.version)

    # T29 裁判装配：题集里有该裁判的题时，裁判模型必须在跑前就位
    # （JUDGE_MODEL 未配置在此显式失败，R7——不许跑完 17 题才发现裁判没配）
    judge_fn: JudgeFn | None = None
    if any(
        i.category in ("normal", "edge") and i.judge == "model"
        and i.item_id != OVERSIZE_ITEM_ID
        for i in items
    ):
        judge_model = judge_model_from_env()
        judge_tpl = load_judge_template()

        def judge_fn(  # type: ignore[no-redef]
            item: GoldenItem, result: QaResult, hits: list[ChunkHit]
        ) -> JudgeOutcome:
            return judge_answer(
                question=item.question,
                expected_answer=item.expected_answer,
                hits=hits,
                actual_answer=result.answer or "",
                model=judge_model,
                template=judge_tpl,
            )

    engine = create_engine(os.environ["DATABASE_URL"])
    wall_start = time.perf_counter()
    with Session(engine) as session:
        dataset_id = ensure_dataset(session, path=GOLDEN_SET_PATH, items=items)
        run_id = eval_repo.create_run(
            session,
            dataset_id=dataset_id,
            git_commit=commit,
            config_snapshot=config_snapshot(args.kb_id),
            model_config_ids=None,
        )
        print(
            f"评估开始：run_id={run_id} 数据集={DATASET_NAME}@{DATASET_VERSION}"
            f"（id={dataset_id}）共 {len(items)} 题，kb_id={args.kb_id}",
            file=sys.stderr,
        )
        try:
            outcomes = run_all_items(
                session, items, kb_id=args.kb_id, judge=judge_fn
            )
        except Exception as exc:
            # R7：显式失败——run 落 failed + 原因，不上交一份"半成品的漂亮表格"
            eval_repo.finish_run(
                session, run_id, status="failed", metrics=None,
                error_reason=f"{type(exc).__name__}: {exc}"[:500],
            )
            raise

        metrics = aggregate(outcomes)
        report_path, failures_path = write_reports(run_id, commit, outcomes, metrics)
        # items 与 finish_run 同事务（insert 不 commit，finish 内部 commit）——
        # 逐条结果与汇总指标同生共死（eval_repo 事务约定）
        eval_repo.insert_run_items(
            session, run_id, [o.to_row(run_id) for o in outcomes]
        )
        eval_repo.finish_run(
            session,
            run_id,
            status="done",
            metrics=metrics,
            cost_cny=Decimal("0"),
            total_tokens_in=metrics["tokens"]["prompt"],
            total_tokens_out=metrics["tokens"]["completion"],
            wall_time_s=round(time.perf_counter() - wall_start),
            report_path=str(report_path.relative_to(PROJECT_ROOT)),
        )

    print_table(metrics)
    pending = [o.item.item_id for o in outcomes if o.item.judge == "human"]
    if pending:
        print(
            f"\n待人工评分（judge: human，共 {len(pending)} 题）："
            f"{', '.join(pending)}"
        )
    print(f"\n报告：{report_path.relative_to(PROJECT_ROOT)}")
    if failures_path:
        print(f"失败案例：{failures_path.relative_to(PROJECT_ROOT)}")
    print(f"run_id={run_id}（查库：SELECT * FROM eval_runs WHERE id={run_id};）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
