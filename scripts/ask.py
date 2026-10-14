"""V-1 命令行问答脚本：嵌入 → 检索 → 生成 → 打印答案与引用。

用法：
    python scripts/ask.py "工作满10年年假有多少天" [--top-k 5]

★ V-1 说明：
- 本脚本在宿主机运行，与 scripts/ingest.py 同一套环境约定：
  .env 里 DATABASE_URL 的 compose 服务名 postgres 会被替换为 localhost。
- 比 ingest.py 多一步：把项目根加入 sys.path——不设置 PYTHONPATH
  也能直接 `python scripts/ask.py ...`（ingest 目前由测试带着 PYTHONPATH 调用）。
- 输出约定（T09 冒烟测试断言依赖，勿随意改）：
  · 拒答：stdout 恰好只有固定拒答句一行，退出码 0；
  · 正常：stdout 打印答案 + 「引用：」区块，退出码 0；
  · 模型/嵌入不可用：诊断走 stderr，退出码非 0。
- user_scope_keys V-1 固定 ["internal"]，真实权限在 V0.1 T15 启用。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 免 PYTHONPATH：把项目根加入 sys.path，脚本即可直接运行
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.embedding import EmbeddingError  # noqa: E402
from app.core.llm_gateway import GatewayError  # noqa: E402
from app.services.qa import answer_question  # noqa: E402
from scripts.ingest import load_env  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="V-1 命令行问答脚本")
    parser.add_argument("question", help="要提问的问题")
    parser.add_argument("--top-k", type=int, default=5, help="检索条数（默认 5）")
    args = parser.parse_args()

    if not args.question.strip():
        print("问题不能为空（R7）", file=sys.stderr)
        return 2

    # 与 Makefile test 同一套约定：读项目根 .env 注入环境，postgres → localhost。
    # setdefault：显式设置的环境变量优先，.env 只兜底。
    for key, value in load_env().items():
        os.environ.setdefault(key, value)

    # qa/generator 的诊断与告警统一走 stderr，stdout 只承载答案
    try:
        result = answer_question(
            args.question, top_k=args.top_k, user_scope_keys=["internal"]
        )
    except (EmbeddingError, GatewayError) as exc:
        # 模型/嵌入不可用：显式失败（R7），诊断走 stderr，非零退出
        print(f"模型服务不可用：{exc}", file=sys.stderr)
        return 1

    print(
        f"（检索 {result.retrieve_ms}ms / 生成 {result.generate_ms}ms）",
        file=sys.stderr,
    )
    print(result.answer)
    if result.refused:
        return 0

    if result.citations:
        print("\n引用：")
        for c in result.citations:
            print(
                f"[{c.index}]《{c.doc_title}》 chunk#{c.chunk_id}"
                f"（得分 {c.score:.4f}）"
            )
            print(f"    {c.snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
