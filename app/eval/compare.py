"""T31 评估对比工具：两次 run 间的指标差 + 翻转题清单。

核心边界（eval.md 二节 / R14）：
  - 两次 run 的 config_snapshot 必须"主链路一致"才允许对比，
    否则抛 ConfigDiffError（不可归因）。
  - 题目集合必须完全相同（item_id 集合 = item_id 集合），
    否则抛 ItemMismatchError。

分层：
  - 纯函数：detect_config_diffs / compare_metrics / diff_items —— 可单测；
  - 真机：compare_runs —— 拉 DB；
  - CLI：main —— make eval-compare 入口。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.repos import eval_repo
from app.repos.models import EvalRun

# ── 异常 ────────────────────────────────────────────────────────────────────


class ConfigDiffError(ValueError):
    """两次 run 的 config_snapshot 有硬差异，禁止直接对比（eval.md 二节）。"""


class DatasetChecksumError(ValueError):
    """两次 run 用的数据集 checksum 不同——题目/标准答案已变，不可归因。

    触发条件：eval_datasets.checksum 不匹配（README 92 行 + R14）。
    """


class ItemMismatchError(ValueError):
    """两次 run 的 item_id 集合不一致——题目集合不同，无法逐题 diff。"""


# ── 硬差异白名单（一变就拒绝对比） ───────────────────────────────────────────
# 这些 key 直接决定检索/分块/生成的行为，一变指标就不可归因。
# 明确排除（只告警不拒绝）：
#   - judge_model（裁判层，不属于 RAG 主链路；run 97→149 变了 14b→14b-8k）
#   - dataset（版本号字符串；真正的数据集一致性靠 eval_datasets.checksum
#     前置硬校验——checksum 不同直接抛 DatasetChecksumError，不走这里）
HARD_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "prompt_version",
        "top_k",
        "chunk_size",
        "chunk_overlap",
        "embedding_model",
        "llm_model",
        "rerank",
        "hybrid",
        "eval_kb_id",
        "identity_map",
    }
)

# 白名单外但允许对比的 key 差（软差异，只告警不拒绝）
_SOFT_CONFIG_KEYS: frozenset[str] = frozenset({"judge_model", "dataset"})


# ── 数据类 ──────────────────────────────────────────────────────────────────


@dataclass
class ConfigDiff:
    """一条硬差异：key / baseline 值 / target 值。"""

    key: str
    baseline: Any
    target: Any


@dataclass
class MetricDiff:
    """一条指标差：dotted key / baseline / target / Δ。"""

    key: str
    baseline: Decimal
    target: Decimal
    delta: Decimal


@dataclass
class ItemFlip:
    """一条题目翻转：item_id / baseline status → target status / verdict 变化。"""

    item_id: str
    baseline_status: str | None
    target_status: str | None
    baseline_verdict: str | None
    target_verdict: str | None


@dataclass
class CompareResult:
    """一次完整对比的产物。"""

    baseline_run_id: int
    target_run_id: int
    hard_diffs: list[ConfigDiff] = field(default_factory=list)
    soft_diffs: list[ConfigDiff] = field(default_factory=list)
    metric_diffs: list[MetricDiff] = field(default_factory=list)
    flips: list[ItemFlip] = field(default_factory=list)
    added_item_ids: list[str] = field(default_factory=list)
    removed_item_ids: list[str] = field(default_factory=list)


# ── 纯函数 ──────────────────────────────────────────────────────────────────


def detect_config_diffs(
    baseline_cfg: dict, target_cfg: dict
) -> tuple[list[ConfigDiff], list[ConfigDiff]]:
    """比较两份 config_snapshot。

    返回 (硬差异列表, 软差异列表)。硬差异非空时调用方应拒绝继续。
    """
    hard: list[ConfigDiff] = []
    soft: list[ConfigDiff] = []
    all_keys = set(baseline_cfg) | set(target_cfg)
    for key in all_keys:
        b_val = baseline_cfg.get(key)
        t_val = target_cfg.get(key)
        if b_val == t_val:
            continue
        diff = ConfigDiff(key=key, baseline=b_val, target=t_val)
        if key in HARD_CONFIG_KEYS:
            hard.append(diff)
        elif key in _SOFT_CONFIG_KEYS:
            soft.append(diff)
        # 未知 key 也当软差异——runner 以后可能加字段，不是主链路硬变更
        else:
            soft.append(diff)
    return hard, soft


def _flatten_metrics(obj: Any, prefix: str = "") -> dict[str, Decimal]:
    """把 metrics 嵌套 JSONB 拍平成 {dotted.key: Decimal}。

    只保留 int / float / Decimal 叶子节点；跳过 str / bool / null / dict / list。
    """
    result: dict[str, Decimal] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            result.update(_flatten_metrics(v, child))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        result[prefix] = Decimal(str(obj))
    elif isinstance(obj, Decimal):
        result[prefix] = obj
    return result


def compare_metrics(
    baseline_metrics: dict, target_metrics: dict
) -> list[MetricDiff]:
    """比较两次 run 的指标嵌套 JSONB。

    只比较**双方都有**的 key（避免一侧新增字段让 Δ 无意义）。
    """
    b = _flatten_metrics(baseline_metrics or {})
    t = _flatten_metrics(target_metrics or {})
    diffs: list[MetricDiff] = []
    for key in sorted(set(b) & set(t)):
        delta = t[key] - b[key]
        # 跳过完全相同的——终端表要聚焦差异
        if delta != 0:
            diffs.append(
                MetricDiff(key=key, baseline=b[key], target=t[key], delta=delta)
            )
    return diffs


def diff_items(
    baseline_items: Iterable[Any], target_items: Iterable[Any]
) -> tuple[list[ItemFlip], list[str], list[str]]:
    """逐题对比两次 run 的 item。

    baseline_items / target_items 是 EvalRunItem ORM 对象或任何带 item_id 属性的。
    返回 (翻转清单, baseline 有 target 没有的 item_id,
           target 有 baseline 没有的 item_id)。
    """
    b_map = {i.item_id: i for i in baseline_items}
    t_map = {i.item_id: i for i in target_items}
    b_ids = set(b_map)
    t_ids = set(t_map)

    added = sorted(t_ids - b_ids)
    removed = sorted(b_ids - t_ids)
    common_ids = sorted(b_ids & t_ids)

    flips: list[ItemFlip] = []
    for item_id in common_ids:
        bi = b_map[item_id]
        ti = t_map[item_id]
        b_status = getattr(bi, "status", None)
        t_status = getattr(ti, "status", None)
        b_verdict = getattr(bi, "verdict", None)
        t_verdict = getattr(ti, "verdict", None)
        if b_status != t_status or b_verdict != t_verdict:
            flips.append(
                ItemFlip(
                    item_id=item_id,
                    baseline_status=b_status,
                    target_status=t_status,
                    baseline_verdict=b_verdict,
                    target_verdict=t_verdict,
                )
            )
    return flips, added, removed


# ── 真机主函数 ──────────────────────────────────────────────────────────────


def compare_runs(
    engine: Engine, baseline_run_id: int, target_run_id: int
) -> CompareResult:
    """拉 DB 对比两次 run。

    Raises:
        DatasetChecksumError: eval_datasets.checksum 不同（数据集内容变了，不可归因）。
        ConfigDiffError: config_snapshot 有硬差异（prompt/检索/分块/生成 主链路变了）。
        ItemMismatchError: item_id 集合不一致。
        RunNotFoundError: run_id 不存在（eval_repo 显式失败）。
    """
    with Session(engine) as session:
        b_run, b_items = eval_repo.get_run_with_items(session, baseline_run_id)
        t_run, t_items = eval_repo.get_run_with_items(session, target_run_id)

        # ★ 0. eval_datasets.checksum 前置硬校验（README 92 行 + R14）
        #     这是比 config_snapshot 更深层的保障——checksum 不同意味着
        #     golden_set.yaml 内容变了（题目/标准答案/类别），
        #     两次 run 指标差异不可归因，直接拒绝。
        b_ck = eval_repo.get_dataset_checksum(session, b_run.dataset_id)
        t_ck = eval_repo.get_dataset_checksum(session, t_run.dataset_id)
        if b_ck is None or t_ck is None:
            raise DatasetChecksumError(
                "至少一条 run 的 dataset 未注册 checksum（eval_datasets 表缺失）"
            )
        if b_ck != t_ck:
            raise DatasetChecksumError(
                f"数据集 checksum 不同——baseline={b_ck[:12]}... "
                f"target={t_ck[:12]}...：题目/标准答案已变，不可归因（README 92 行）。"
            )

    # ① config_snapshot 对比
    hard, soft = detect_config_diffs(
        b_run.config_snapshot or {}, t_run.config_snapshot or {}
    )
    if hard:
        diff_desc = ", ".join(f"{d.key}: {d.baseline!r}→{d.target!r}" for d in hard)
        raise ConfigDiffError(
            f"config_snapshot 硬差异，不可归因（eval.md 二节）：{diff_desc}"
        )

    # ② item_id 集合一致性
    flips, added, removed = diff_items(b_items, t_items)
    if added or removed:
        raise ItemMismatchError(
            f"题目集合不一致：baseline 有 {removed}、target 有 {added}，"
            "无法逐题 diff。"
        )

    # ③ 指标差
    metric_diffs = compare_metrics(b_run.metrics or {}, t_run.metrics or {})

    return CompareResult(
        baseline_run_id=baseline_run_id,
        target_run_id=target_run_id,
        hard_diffs=hard,
        soft_diffs=soft,
        metric_diffs=metric_diffs,
        flips=flips,
        added_item_ids=added,
        removed_item_ids=removed,
    )


# ── 格式化输出 ──────────────────────────────────────────────────────────────


def _fmt_metric(d: MetricDiff) -> str:
    delta = d.delta
    sign = "+" if delta > 0 else ""
    # float 类指标保留 3 位小数；ms 类保留整数
    val_str = f"{delta:.3f}" if abs(delta) < Decimal("1") else f"{delta:.0f}"
    arrow = "↓" if delta < 0 else ("↑" if delta > 0 else " ")
    color = "\033[32m" if delta > 0 else ("\033[31m" if delta < 0 else "")
    reset = "\033[0m" if color else ""
    return f"{arrow} {color}{sign}{val_str}{reset}"


def format_result(result: CompareResult) -> str:
    """把 CompareResult 渲染成终端可读的 ASCII 表 + 色标。"""
    lines: list[str] = []
    b = result.baseline_run_id
    t = result.target_run_id
    lines.append(f"评估对比：run {b}（基线） vs run {t}（目标）")
    lines.append("=" * 60)

    # config 软差异
    if result.soft_diffs:
        lines.append("⚠️  config_snapshot 软差异（不影响归因，但请留意）：")
        for cd in result.soft_diffs:
            lines.append(f"   {cd.key}: {cd.baseline!r} → {cd.target!r}")
        lines.append("")

    # 指标表
    lines.append("指标差（只列有变化的）：")
    lines.append(f"  {'指标':<36} {'基线':>10} {'目标':>10} {'Δ':>12}")
    lines.append("  " + "-" * 70)
    for md in result.metric_diffs:
        b_str = f"{md.baseline:.3f}" if abs(md.baseline) < 1 else f"{md.baseline:.0f}"
        t_str = f"{md.target:.3f}" if abs(md.target) < 1 else f"{md.target:.0f}"
        lines.append(f"  {md.key:<36} {b_str:>10} {t_str:>10} {_fmt_metric(md):>12}")
    if not result.metric_diffs:
        lines.append("  （无变化）")
    lines.append("")

    # 翻转题
    lines.append(f"题目翻转（共 {len(result.flips)} 条）：")
    if result.flips:
        lines.append(
            f"  {'item_id':<12} {'基线':<10} {'目标':<10}"
            f" {'verdict 基线':<14} {'verdict 目标':<14}"
        )
        lines.append("  " + "-" * 62)
        for f in result.flips:
            lines.append(
                f"  {f.item_id:<12} {str(f.baseline_status or '-'):<10}"
                f" {str(f.target_status or '-'):<10}"
                f" {str(f.baseline_verdict or '-'):<14}"
                f" {str(f.target_verdict or '-'):<14}"
            )
    else:
        lines.append("  （无翻转）")

    return "\n".join(lines)


# ── CLI 入口 ────────────────────────────────────────────────────────────────


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="T31 评估对比：两次 run 间的指标差 + 翻转题清单",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--auto",
        action="store_true",
        help="自动找基线 vs 最新完成 run",
    )
    g.add_argument("--baseline", type=int, help="基线 run_id")
    p.add_argument("--target", type=int, help="目标 run_id（和 --baseline 一起传）")
    p.add_argument(
        "--no-color", action="store_true", help="禁用终端 ANSI 色标（输出贴文档时用）"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    conn = os.environ.get("DATABASE_URL")
    if not conn:
        print(
            "错误：未设置 DATABASE_URL（请通过 make eval-compare 运行）",
            file=sys.stderr,
        )
        return 4
    engine = create_engine(conn, pool_pre_ping=True)

    with Session(engine) as session:
        if args.auto:
            b_run = eval_repo.find_baseline(session)
            if b_run is None:
                print(
                    "错误：尚未设置基线 run（eval_runs.is_baseline=true）",
                    file=sys.stderr,
                )
                return 1
            t_run = (
                session.query(EvalRun)
                .where(EvalRun.status == "done")
                .order_by(EvalRun.id.desc())
                .limit(1)
                .one_or_none()
            )
            if t_run is None:
                print("错误：没有已完成（done）的 run 可对比", file=sys.stderr)
                return 1
            b_id, t_id = int(b_run.id), int(t_run.id)
        else:
            if args.target is None:
                print("错误：--baseline 和 --target 必须一起传", file=sys.stderr)
                return 1
            b_id, t_id = args.baseline, args.target

    try:
        result = compare_runs(engine, b_id, t_id)
    except ConfigDiffError as exc:
        print(f"❌ 硬差异：{exc}", file=sys.stderr)
        return 1
    except ItemMismatchError as exc:
        print(f"❌ 题目集合不一致：{exc}", file=sys.stderr)
        return 2
    except eval_repo.RunNotFoundError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 3

    text = format_result(result)
    if args.no_color:
        # 简单剥掉 ANSI 码——用正则最干净
        import re

        text = re.sub(r"\033\[[0-9;]*m", "", text)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
