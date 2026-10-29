"""T82 时间语义解析单元测试：纯函数，不联网、不碰数据库。

覆盖三条验收面：① 时间错配 → 拒答；② 时间匹配的正常题不被误拒；③ 无时间词不影响。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.repos.chunk_repo import ChunkHit
from app.services.time_semantics import (
    check_time_mismatch,
    extract_doc_years,
    parse_question_years,
)

# 固定基准日，避免跨年时单测结果漂移
TODAY = date(2026, 10, 29)


def _hit(
    content: str, title: str = "公司2026年战略目标.txt", chunk_id: int = 1
) -> ChunkHit:
    return ChunkHit(
        chunk_id=chunk_id,
        content=content,
        doc_id=1,
        doc_title=title,
        char_start=0,
        char_end=len(content),
        score=0.9,
    )


def test_question_years_relative_words():
    assert parse_question_years("明年的战略目标是什么", TODAY) == {2027}
    assert parse_question_years("今年的战略目标", TODAY) == {2026}
    assert parse_question_years("去年的营收", TODAY) == {2025}
    assert parse_question_years("前年的营收", TODAY) == {2024}
    assert parse_question_years("后年的规划", TODAY) == {2028}


def test_question_years_ignore_non_year_words():
    """"年假/10 年/几年/年度"不是年份表达，不得触发判定。"""
    assert parse_question_years("年假怎么算啊", TODAY) == set()
    assert parse_question_years("工作满 10 年年假多少天？", TODAY) == set()
    assert parse_question_years("劳动合同一般签几年", TODAY) == set()
    assert parse_question_years("公司有年度体检吗", TODAY) == set()
    assert parse_question_years("Q3 发布了哪些新产品", TODAY) == set()


def test_question_explicit_year():
    assert parse_question_years("2026 年的战略目标是什么", TODAY) == {2026}
    assert parse_question_years("问一下 2027年 的预算", TODAY) == {2027}


def test_extract_doc_years_from_content_and_title():
    assert extract_doc_years("版本：V1.0 发布日期：2026 年 1 月 15 日") == {2026}
    assert extract_doc_years("", "产品发布公告2026Q3.txt") == {2026}
    # 非年份的 4 位数字（金额）不得被当成年份
    assert extract_doc_years("其中餐费 1204 元，住宿 2026") == set()


def test_next_year_vs_2026_doc_is_mismatch():
    """Q027：问"明年"（2027），资料只有 2026 → 判错配。"""
    hits = [
        _hit("# 公司 2026 年战略目标\n本文件明确公司 2026 年的战略目标", chunk_id=1),
        _hit("生效日期：2026 年 2 月 1 日", title="信息安全管理制度.txt", chunk_id=2),
    ]
    check = check_time_mismatch("明年的战略目标是什么", hits, today=TODAY)

    assert check.mismatched is True
    assert check.reason == "mismatch"
    assert check.question_years == {2027}
    assert check.doc_years == {2026}


def test_this_year_matches_2026_doc():
    hits = [_hit("# 公司 2026 年战略目标", chunk_id=1)]
    check = check_time_mismatch("今年的战略目标是什么", hits, today=TODAY)

    assert check.mismatched is False
    assert check.reason == "matched"


def test_explicit_year_matches_doc():
    hits = [_hit("发布日期：2026 年 1 月 15 日", chunk_id=1)]
    check = check_time_mismatch("2026 年的战略目标是什么", hits, today=TODAY)

    assert check.mismatched is False
    assert check.reason == "matched"


@pytest.mark.parametrize(
    "question",
    ["年假怎么算啊", "工作满 10 年年假多少天？", "Q3 发布了哪些新产品"],
    ids=["no_time_word", "duration_not_year", "quarter_only"],
)
def test_no_time_expression_not_checked(question: str):
    """无年份词 → 完全不做时间判定（正常题不得被误拒）。"""
    hits = [_hit("# 公司 2026 年战略目标", chunk_id=1)]
    check = check_time_mismatch(question, hits, today=TODAY)

    assert check.mismatched is False
    assert check.reason == "no_time_expression"
    assert check.question_years == frozenset()


def test_no_doc_year_anchor_passes_with_reason():
    """问题有年份词但资料没写年份 → 放行（无法验证不硬拒）。"""
    hits = [_hit("年假：满 1 年 5 天，满 3 年 10 天", title="员工手册.txt", chunk_id=1)]
    check = check_time_mismatch("明年的年假政策会改吗", hits, today=TODAY)

    assert check.mismatched is False
    assert check.reason == "no_doc_year_anchor"
    assert check.doc_years == frozenset()
