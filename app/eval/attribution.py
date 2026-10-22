"""归因候选生成器（`make eval-attribute`，attribution.md 第二/三节）。

做什么：对指定 run（默认最新已收尾 run）的 fail 条目，只跑检索路径
（embed + search_chunks，不调生成模型，单条成本≈0），回填
`retrieval_hit`，再按判定表套出 `attribution_candidate` 落库。

职责边界：
  ★ R15：本工具**绝不写 `attribution_confirmed`**——结论只能人工写入，
    候选只是分组，不进任何统计。
  ★ R14：跑前校验该 run 的 dataset checksum 与当前 golden_set 文件
    一致，不一致**拒绝开始**（题目变了，用新标尺给旧 run 归因没有意义）。
  ★ 与 runner 同一检索口径：kb 轴锁定评估语料库（默认 534），scope 走
    题目设定提问者的真实范围——不锁定会被演示文档污染（runner.py 头注）。

用法：.venv/bin/python -m app.eval.attribution [--run-id N] [--kb-id 534]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.embedding import embed_texts
from app.eval import loader
from app.repos import eval_repo
from app.repos.chunk_repo import search_chunks
from app.repos.models import EvalRun
from app.services.permission import resolve_permission

GOLDEN_SET_PATH = Path("evals/golden_set.yaml")
DEFAULT_KB_ID = 534  # 与 runner.DEFAULT_KB_ID 一致：评估语料库

# 候选值集合（data-model 2.13）：retrieval_suspect / generation_suspect / unclassified
RETRIEVAL_SUSPECT = "retrieval_suspect"
GENERATION_SUSPECT = "generation_suspect"
UNCLASSIFIED = "unclassified"


def attribution_candidate(
    *,
    refused: bool | None,
    retrieval_hit: bool | None,
    status: str,
    should_refuse: bool,
    verdict: str | None,
    faithfulness: float | None,
) -> str:
    """候选归因判定表（attribution.md 第三节）。纯函数，行列级可测。

    ★ 只产候选，不产结论。`unclassified` 允许大量存在——一个诚实的
      "未归类"比一个编造的结论有价值（attribution.md 第三节末注）。

    规则顺序即优先级（命中即返回）：
      1. status=error → unclassified（先看工程错误，不参与质量归因）
      2. should_refuse=true 且 refused=false → generation_suspect
         （该拒未拒：拒答判定在生成侧。2026-10-22 补：覆盖 hit=null 的
         应拒题——它们没有 expected_sources，retrieval_hit 算不出来，
         按旧表会全部掉进 unclassified，Q027 实证）
      3. refused=true 且 retrieval_hit=true → generation_suspect
         （资料在手却没答，疑似拒答阈值/引用绑定问题）
      4. refused=false 且 retrieval_hit=false 且 faithfulness<0.85
         → retrieval_suspect（没找到资料却硬答，召回失败叠加幻觉）
      5. refused=false 且 retrieval_hit=true 且 verdict ∈
         {off_topic, wrong_number} → generation_suspect（答偏/答错数字）
      6. 其余 → unclassified
    """
    if status == "error":
        return UNCLASSIFIED
    if should_refuse and refused is False:
        return GENERATION_SUSPECT
    if refused and retrieval_hit:
        return GENERATION_SUSPECT
    if (
        refused is False
        and retrieval_hit is False
        and faithfulness is not None
        and faithfulness < 0.85
    ):
        return RETRIEVAL_SUSPECT
    if refused is False and retrieval_hit and verdict in ("off_topic", "wrong_number"):
        return GENERATION_SUSPECT
    return UNCLASSIFIED


def _rerun_retrieval(
    session: Session,
    item: loader.GoldenItem,
    *,
    kb_id: int,
) -> bool:
    """只跑检索路径，返回标准来源文档是否出现在 Top-5（文档级命中）。

    与 runner 同口径：scope 走题目设定提问者的真实范围，kb_ids 强制
    [kb_id]。判“文档级”与 runner._score_chain_item 的 Recall@5 一致
    （expected_doc_titles 去掉 # 锚点，锚点留人工核对）。
    """
    perm = resolve_permission(session, username=item.username)
    query_vec = embed_texts([item.question])[0]
    hits = search_chunks(query_vec, perm.scope_keys, top_k=5, kb_ids=[kb_id])
    expected_titles = set(item.expected_doc_titles)
    return any(h.doc_title in expected_titles for h in hits)


def attribute_run(
    session: Session, run_id: int, *, kb_id: int = DEFAULT_KB_ID, version: str = "V0.1"
) -> int:
    """对一个 run 的 fail 条目跑归因候选回填，返回处理条数。

    version 是题目代际过滤条件（与 runner 的 --version 同义，默认 V0.1）——
    与数据集注册版本（1.0.x）是两个轴：checksum 门禁已保证文件一致，
    这里只需要按代际取出与 run 当时相同的题目集合。

    Raises:
        SystemExit: dataset checksum 与当前 golden_set 不一致（R14 拒绝开始）；
            run 不存在（eval_repo.RunNotFoundError 由调用方转成退出码）。
    """
    run = eval_repo.get_run(session, run_id)
    dataset = eval_repo.get_dataset(session, run.dataset_id)

    # R14：题目变了就不能用新标尺给旧 run 归因
    current_checksum = loader.file_checksum(GOLDEN_SET_PATH)
    if current_checksum != dataset.checksum:
        raise SystemExit(
            f"数据集 checksum 不一致，归因拒绝开始（R14）：\n"
            f"  run {run_id} 用的是 {dataset.name} v{dataset.version}"
            f"（checksum={dataset.checksum[:12]}…）\n"
            f"  当前 golden_set.yaml checksum={current_checksum[:12]}…\n"
            f"题目已变更，用新标尺给旧 run 归因没有意义。"
        )

    items = {
        i.item_id: i for i in loader.load_golden_set(GOLDEN_SET_PATH, version=version)
    }
    fails = eval_repo.list_run_items(session, run_id, status="fail")
    if not fails:
        print(f"[attribute] run {run_id} 没有 fail 条目，无需归因", file=sys.stderr)
        return 0

    processed = 0
    for row in fails:
        golden = items.get(row.item_id)
        if golden is None:
            # 题集里已没有这道题（题目被删/改 id）——候选只能是 unclassified，
            # 但仍落库，让"未确认 N 条"的统计口径完整。
            eval_repo.update_attribution_candidate(
                session, run_id, row.item_id,
                retrieval_hit=row.retrieval_hit, candidate=UNCLASSIFIED,
            )
            print(
                f"  {row.item_id} 不在当前题集，candidate=unclassified",
                file=sys.stderr,
            )
            processed += 1
            continue

        # 有标准来源的题重跑检索；没有的（refuse 类）hit 保持 None——
        # “库里没有答案”无从谈命中，这不是缺口，是语义本身。
        retrieval_hit = row.retrieval_hit
        if golden.expected_doc_titles:
            retrieval_hit = _rerun_retrieval(session, golden, kb_id=kb_id)

        faithfulness = (row.scores or {}).get("faithfulness")
        candidate = attribution_candidate(
            refused=row.refused,
            retrieval_hit=retrieval_hit,
            status=row.status,
            should_refuse=golden.should_refuse,
            verdict=row.verdict,
            faithfulness=faithfulness,
        )
        eval_repo.update_attribution_candidate(
            session, run_id, row.item_id,
            retrieval_hit=retrieval_hit, candidate=candidate,
        )
        print(
            f"  {row.item_id} refused={row.refused} hit={retrieval_hit} "
            f"→ candidate={candidate}",
            file=sys.stderr,
        )
        processed += 1

    return processed


def _latest_finished_run_id(session: Session) -> int:
    """最近一个已收尾（done/failed/cancelled）的 run id；没有则显式失败。"""
    run_id = session.execute(
        select(EvalRun.id)
        .where(EvalRun.status.in_(["done", "failed", "cancelled"]))
        .order_by(EvalRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run_id is None:
        raise SystemExit("没有任何已收尾的评估 run，先跑 make eval")
    return run_id


def main() -> None:
    parser = argparse.ArgumentParser(description="归因候选生成器（只产候选，R15）")
    parser.add_argument("--run-id", type=int, default=None,
                        help="目标 run id（默认最新已收尾 run）")
    parser.add_argument("--kb-id", type=int, default=DEFAULT_KB_ID,
                        help=f"评估语料库 kb_id（默认 {DEFAULT_KB_ID}）")
    parser.add_argument("--version", default="V0.1",
                        help="题目代际（与 runner --version 同义，默认 V0.1）")
    args = parser.parse_args()

    if not os.environ.get("DATABASE_URL"):
        raise SystemExit(
            "DATABASE_URL 未设置（请通过 make eval-attribute 运行，或先 source .env）"
        )
    engine = create_engine(os.environ["DATABASE_URL"])
    with Session(engine) as session:
        run_id = args.run_id
        if run_id is None:
            run_id = _latest_finished_run_id(session)
        processed = attribute_run(
            session, run_id, kb_id=args.kb_id, version=args.version
        )
    print(
        f"[attribute] run {run_id}：{processed} 条 fail 已回填 "
        f"retrieval_hit / attribution_candidate（结论仍需人工确认，R15）",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
