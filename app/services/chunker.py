"""递归字符分块器（V-1）。

策略（见 docs/architecture.md §数据链路）：
  512 字 + 64 字重叠，按段落 → 句子 → 硬切的优先级递归切分。

★ 为什么不用 LangChain 的分割器（AGENTS.md R12 / ADR 0005）：
  分块边界是 RAG 质量的命门，必须能看清"到底切在哪"。
  框架的分割器是黑盒，出了问题无法定位是哪一级分隔符失效。

设计要点：
1. 分隔符从粗到细：段落 → 换行 → 句号/问号/感叹号 → 分号 → 逗号 → 顿号 → 空格 → 硬切。
2. 每个片段长度 ≤ chunk_size；超过的用下一级分隔符递归切。
3. 相邻块之间保留 chunk_overlap 字的重叠（取上一块末尾）。
4. 全程追踪 char_start / char_end，便于引用定位（不靠事后 find）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Chunk:
    """一个分块。"""

    content: str
    chunk_index: int
    char_start: int
    char_end: int
    # V-1 不接 tokenizer，留空；后续加 tokenizer 后回填
    token_count: int | None = None


# 中文场景的分隔符优先级（从粗到细）
DEFAULT_SEPARATORS: list[str] = [
    "\n\n",  # 段落
    "\n",  # 换行
    "。",  # 句号
    "！",  # 感叹号
    "？",  # 问号
    "；",  # 分号
    "，",  # 逗号
    "、",  # 顿号
    " ",  # 空格
    "",  # 硬切（兜底）
]


def chunk_text(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 64,
    separators: list[str] | None = None,
) -> list[Chunk]:
    """把文本切分成若干块。

    Args:
        text: 输入文本。
        chunk_size: 每块最大字符数（不含重叠）。
        chunk_overlap: 相邻块重叠字符数，必须 < chunk_size。
        separators: 自定义分隔符优先级，默认用 DEFAULT_SEPARATORS。

    Returns:
        分块列表，按 chunk_index 升序。
    """
    if not text:
        return []
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须小于 chunk_size")

    seps = separators if separators is not None else DEFAULT_SEPARATORS

    # 1. 递归分割成小片段（每段 ≤ chunk_size），追踪字符位置
    splits: list[tuple[str, int, int]] = _recursive_split(
        text, 0, len(text), seps, chunk_size
    )

    # 2. 合并小片段成大块，相邻块间保留重叠
    merged: list[tuple[str, int, int]] = _merge_splits(
        splits, chunk_size, chunk_overlap
    )

    # 3. 组装结果
    return [
        Chunk(content=c, chunk_index=i, char_start=s, char_end=e)
        for i, (c, s, e) in enumerate(merged)
    ]


def _recursive_split(
    text: str,
    start: int,
    end: int,
    separators: list[str],
    chunk_size: int,
) -> list[tuple[str, int, int]]:
    """递归分割 [start, end)，返回 [(content, start, end), ...]，每段 ≤ chunk_size。"""
    length = end - start
    if length == 0:
        return []
    if length <= chunk_size:
        return [(text[start:end], start, end)]

    sep = separators[0]
    remaining = separators[1:]

    if sep == "":
        # 硬切兜底：按字符切
        result: list[tuple[str, int, int]] = []
        for i in range(start, end, chunk_size):
            e = min(i + chunk_size, end)
            result.append((text[i:e], i, e))
        return result

    # 按当前分隔符切分，分隔符附在片段末尾
    parts: list[tuple[int, int]] = _split_keep_separator(text, start, end, sep)

    result = []
    cur_start = start
    cur_end = start
    for part_start, part_end in parts:
        part_len = part_end - part_start
        cur_len = cur_end - cur_start
        if cur_len + part_len <= chunk_size:
            cur_end = part_end
        else:
            if cur_end > cur_start:
                result.append((text[cur_start:cur_end], cur_start, cur_end))
            if part_len > chunk_size and remaining:
                # 单段超长，用下一级分隔符递归切
                result.extend(
                    _recursive_split(text, part_start, part_end, remaining, chunk_size)
                )
                cur_start = part_end
                cur_end = part_end
            else:
                cur_start = part_start
                cur_end = part_end

    if cur_end > cur_start:
        result.append((text[cur_start:cur_end], cur_start, cur_end))
    return result


def _split_keep_separator(
    text: str, start: int, end: int, sep: str
) -> list[tuple[int, int]]:
    """按 sep 切分 [start, end)，分隔符附在每段末尾。返回 [(s, e), ...]。"""
    if not sep:
        return [(start, end)]

    result: list[tuple[int, int]] = []
    pos = start
    sep_len = len(sep)
    while pos < end:
        idx = text.find(sep, pos, end)
        if idx == -1:
            result.append((pos, end))
            break
        sep_end = idx + sep_len
        result.append((pos, sep_end))
        pos = sep_end
    return result


def _merge_splits(
    splits: list[tuple[str, int, int]],
    chunk_size: int,
    chunk_overlap: int,
) -> list[tuple[str, int, int]]:
    """合并小片段成大块，相邻块间保留 chunk_overlap 字重叠。

    返回 [(content, start, end), ...]。
    """
    result: list[tuple[str, int, int]] = []
    cur_content = ""
    cur_start = 0
    cur_end = 0

    for content, start, end in splits:
        if len(cur_content) + len(content) <= chunk_size:
            if not cur_content:
                cur_start = start
            cur_content += content
            cur_end = end
        else:
            if cur_content:
                result.append((cur_content, cur_start, cur_end))
            # 取下一块的重叠前缀（上一块末尾 chunk_overlap 字）
            if chunk_overlap > 0 and len(cur_content) > chunk_overlap:
                cur_content = cur_content[-chunk_overlap:]
                cur_start = cur_end - chunk_overlap
            else:
                cur_content = ""
                cur_start = start
            cur_content += content
            cur_end = end
            # "重叠前缀 + 新片段"仍超限时继续切（单片段接近 chunk_size 的极端情况）
            while len(cur_content) > chunk_size:
                cut_pos = cur_start + chunk_size
                result.append((cur_content[:chunk_size], cur_start, cut_pos))
                if chunk_overlap > 0:
                    cur_content = cur_content[chunk_size - chunk_overlap :]
                    cur_start = cut_pos - chunk_overlap
                else:
                    cur_content = cur_content[chunk_size:]
                    cur_start = cut_pos

    if cur_content:
        result.append((cur_content, cur_start, cur_end))

    return result
