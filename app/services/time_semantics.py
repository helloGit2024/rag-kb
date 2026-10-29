"""时间语义解析（T82）：生成前的确定性"时间错配"判定。

为什么放在代码里而不是提示词里：Q027 实证——知识库只有《公司2026年战略目标》，
用户问"明年"（2027），小模型仍把 2026 的目标当作"明年的"输出（run 369 refused=false）。
时间比对必须可测、可复现（R14），不能赌模型听话。

范围（经项目所有者确认，2026-10-29）：**只做年份级**。
  问题侧：今年 / 明年 / 去年 / 前年 / 后年 + 明确写出的「20XX 年」。
  资料侧：「20XX 年」「20XXQn」——正文与标题都取（标题如"产品发布公告2026Q3.txt"）。
  季度 / 半年 / 月份**不解析**：Q018「Q3 发布了哪些新产品」这类正常题因此完全不受影响。

判定规则（先保守，避免误拒）：
  1. 问题没有年份词 → 不检查（正常走完整链路）；
  2. 问题有年份词、但全部检索块都没有年份锚点 → 放行并告警（无法验证就不硬拒）；
  3. 问题年份 ∩ 检索块年份 = ∅ → 判定错配，由调用方按拒答处理（R5）。
★ 年份取**全部检索块**的并集，口径与 T81 的数字校验一致（不是仅被引用块）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.repos.chunk_repo import ChunkHit

# 相对年份词 → 相对基准年的偏移。只收年份级；
# "当年/年度/年假/年薪"这类不是年份表达，刻意不收，避免把正常题判错。
_RELATIVE_YEARS = {
    "前年": -2,
    "去年": -1,
    "今年": 0,
    "明年": 1,
    "后年": 2,
}

# 问题里的明确年份：必须是 4 位年份 + "年"。
# "工作满 10 年年假多少天"里的"10 年"、"试用期是几个月"都不匹配（前向断言 19|20 开头）。
_QUESTION_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年")

# 资料年份锚点："2026 年" / "2026Q3"（正文发布日期、适用期，标题里的年份与季度编号）。
_DOC_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?=\s*(?:年|[Qq][1-4]))")


@dataclass(frozen=True)
class TimeCheck:
    """一次时间比对的结果。

    reason 取值（便于日志与单测断言）：
      no_time_expression   问题里没有年份词，未做检查
      no_doc_year_anchor   问题有年份词，但检索块里没有任何年份锚点（放行）
      matched              问题年份与资料年份有交集（放行）
      mismatch             完全不相交（错配 → 拒答）
    """

    question_years: frozenset[int]
    doc_years: frozenset[int]
    mismatched: bool
    reason: str


def parse_question_years(question: str, today: date | None = None) -> set[int]:
    """解析问题里的年份词，返回年份集合（空集 = 问题不含年份表达）。

    Args:
        question: 用户问题。
        today: 基准日，仅测试注入；线上用 `date.today()`
            （解析结果会打进日志，见 generator）。
    """
    base = (today or date.today()).year
    years: set[int] = set()
    for word, offset in _RELATIVE_YEARS.items():
        if word in question:
            years.add(base + offset)
    for match in _QUESTION_YEAR_RE.finditer(question):
        years.add(int(match.group(1)))
    return years


def extract_doc_years(content: str, title: str = "") -> set[int]:
    """从检索块正文 / 文档标题里抽年份锚点。"""
    years: set[int] = set()
    for text in (content, title):
        if not text:
            continue
        for match in _DOC_YEAR_RE.finditer(text):
            years.add(int(match.group(1)))
    return years


def check_time_mismatch(
    question: str,
    hits: list[ChunkHit],
    *,
    today: date | None = None,
) -> TimeCheck:
    """判断问题时间与检索资料时间是否错配（纯函数，不碰 DB / 模型）。

    Returns:
        TimeCheck。`mismatched=True` 时调用方必须按固定句式拒答（R5）。
    """
    asked = parse_question_years(question, today)
    if not asked:
        return TimeCheck(frozenset(), frozenset(), False, "no_time_expression")

    found: set[int] = set()
    for hit in hits:
        found |= extract_doc_years(hit.content, hit.doc_title)
    if not found:
        # 无法验证就不硬拒：宁可放行走原链路（模型仍可自行拒答），
        # 也不误拒无日期文档的正常题
        return TimeCheck(frozenset(asked), frozenset(), False, "no_doc_year_anchor")

    if asked & found:
        return TimeCheck(frozenset(asked), frozenset(found), False, "matched")
    return TimeCheck(frozenset(asked), frozenset(found), True, "mismatch")
