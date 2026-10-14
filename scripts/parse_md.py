"""T10 验收脚本：解析一份 markdown，打印删除行清单与解析后全文。

用法：
    python scripts/parse_md.py <md 文件路径>

输出约定（诊断走 stderr，与项目其他脚本一致）：
    - 解析后全文 → stdout（可管道存文件）；
    - 被删行清单（页眉页脚判定结果）与统计 → stderr。
- 编码约定：只认 UTF-8；GBK 等编码显式报错（R7），不做自动猜测。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 免 PYTHONPATH：把项目根加入 sys.path，脚本即可直接运行（与 ask.py 同约定）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.services.md_parser import parse_markdown  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("用法：python scripts/parse_md.py <md 文件路径>")

    path = Path(sys.argv[1])
    if not path.is_file():
        sys.exit(f"文件不存在：{path}")

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        # R7 显式失败：不做编码自动猜测，报错说清楚
        sys.exit(f"读取失败：文件不是 UTF-8 编码（{e}）。请先转为 UTF-8 再解析。")

    result = parse_markdown(text)

    print("=== 被删除的页眉页脚行 ===", file=sys.stderr)
    if result.removed_lines:
        for line in result.removed_lines:
            print(f"  - {line}", file=sys.stderr)
    else:
        print("  （无）", file=sys.stderr)
    print(f"=== 解析后全文（{len(result.text)} 字）===", file=sys.stderr)

    print(result.text)


if __name__ == "__main__":
    main()
