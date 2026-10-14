"""Markdown 解析器（V0.1 · T10）。

职责：把 markdown 原文转成喂给分块器（app/services/chunker.py）的纯文本。

四条规则（与 docs/architecture.md §五 的数据流一致）：
1. 去页眉页脚：
   - 页码行：整行匹配「第 X 页 / 第 X 页 共 Y 页 / - X -」形态，直接删；
   - 重复短行：strip 后 ≤ short_line_max_len 字、且全文出现 ≥ repeat_threshold
     次的行，判定为页眉 / 页脚 / 导航行，全部删除。
2. 代码块保护：``` 围栏内的行不参与上述统计与删除
   （代码里重复行是常态，误删 = 破坏原文）。
3. 标题保留：# 标题行原样保留（含 # 前缀）——保留层级信息，
   供后续标题感知分块使用。
4. 空行归一化：≥3 个连续换行压成 2 个。

★ 为什么自研而不引入 markdown 库（R12）：输出目标是「喂分块器的纯文本」，
  不是 HTML/AST，库的核心能力用不上；解析边界是 RAG 质量命门，
  规则必须全部可见可控（与 chunker 同一哲学）。
  代价：复杂 md 语法（嵌套列表、脚注）的边角情况需自行补齐，
  V0.1 接受——用真实 md 验收时暴露什么补什么。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 页码行三种形态：第 X 页 / 第 X 页 共 Y 页 / - X -（含全角变体）
_PAGE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^第\s*\d+\s*页$"),
    re.compile(r"^第\s*\d+\s*页\s*[/,，]?\s*共\s*\d+\s*页$"),
    re.compile(r"^[-–—]\s*\d+\s*[-–—]$"),
]

_CODE_FENCE = re.compile(r"^\s*```")

_BLANK_RUN = re.compile(r"\n{3,}")


@dataclass
class ParseResult:
    """解析结果。

    removed_lines 让验收时能看见「删了什么」，而不只是「剩了什么」。
    """

    text: str
    removed_lines: list[str]  # 被删行（strip 后去重，按首次出现顺序）


def parse_markdown(
    text: str,
    *,
    short_line_max_len: int = 50,
    repeat_threshold: int = 3,
) -> ParseResult:
    """把 markdown 原文解析成纯文本。

    Args:
        text: markdown 原文。
        short_line_max_len: 参与「重复短行」判定的最大长度（strip 后）。
        repeat_threshold: 同一短行至少出现多少次才判定为页眉页脚。
            阈值 3 的盲区：页眉在 2 页文档里只出现 2 次时检测不到。

    Returns:
        ParseResult：纯文本 + 被删行清单。
    """
    if not text:
        return ParseResult(text="", removed_lines=[])

    lines = text.split("\n")
    in_code = _code_block_mask(lines)

    # 统计代码块外的短行出现次数（页眉页脚判定依据）
    counts: dict[str, int] = {}
    for line, masked in zip(lines, in_code, strict=True):
        if masked:
            continue
        stripped = line.strip()
        if 0 < len(stripped) <= short_line_max_len:
            counts[stripped] = counts.get(stripped, 0) + 1

    repeated = {s for s, n in counts.items() if n >= repeat_threshold}

    kept: list[str] = []
    removed: list[str] = []
    removed_seen: set[str] = set()
    for line, masked in zip(lines, in_code, strict=True):
        stripped = line.strip()
        is_junk = not masked and (
            stripped in repeated or _is_page_line(stripped)
        )
        if is_junk:
            if stripped not in removed_seen:
                removed_seen.add(stripped)
                removed.append(stripped)
        else:
            kept.append(line)

    cleaned = _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip("\n")
    return ParseResult(text=cleaned, removed_lines=removed)


def _code_block_mask(lines: list[str]) -> list[bool]:
    """标记每行是否在 ``` 代码围栏内（含围栏行本身）。

    未闭合的围栏：其后内容全部按代码处理（保守保留，不删内容）。
    """
    mask: list[bool] = []
    in_code = False
    for line in lines:
        if _CODE_FENCE.match(line):
            mask.append(True)  # 围栏行自身也保护
            in_code = not in_code
        else:
            mask.append(in_code)
    return mask


def _is_page_line(stripped: str) -> bool:
    """整行（已 strip）是否为页码形态。"""
    return any(p.match(stripped) for p in _PAGE_PATTERNS)
