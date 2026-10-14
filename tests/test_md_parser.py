"""T10 markdown 解析器单元测试（每条解析规则一个可判定用例）。

验收重点（对应 app/services/md_parser.py 的四条规则）：
1. 页码行删除
2. 重复短行（页眉页脚）删除
3. 仅出现 2 次的短行不误删（阈值 3 的边界）
4. 代码块内的行不参与统计、不被删除
5. 标题行原样保留（含 # 前缀）
6. 空行归一化（≥3 连续换行 → 2 个）
"""

from __future__ import annotations

from app.services.md_parser import parse_markdown


def test_page_number_lines_removed():
    text = (
        "正文第一段。\n"
        "第 3 页\n"
        "正文第二段。\n"
        "第 4 页 共 10 页\n"
        "正文第三段。\n"
        "- 5 -\n"
        "正文第四段。\n"
    )
    result = parse_markdown(text)
    assert "第 3 页" not in result.text
    assert "第 4 页 共 10 页" not in result.text
    assert "- 5 -" not in result.text
    assert "正文第四段。" in result.text
    assert set(result.removed_lines) == {"第 3 页", "第 4 页 共 10 页", "- 5 -"}


def test_repeated_short_line_removed():
    text = (
        "某某公司内部资料\n"
        "第一章 总则\n"
        "第一条 为了规范员工行为……\n"
        "某某公司内部资料\n"
        "第二条 员工应当遵守……\n"
        "某某公司内部资料\n"
        "第三条 附则。\n"
    )
    result = parse_markdown(text)
    assert "某某公司内部资料" not in result.text
    assert "第一章 总则" in result.text
    assert "第三条 附则。" in result.text
    assert result.removed_lines == ["某某公司内部资料"]


def test_short_line_twice_kept():
    text = "注意事项\n第一条 内容。\n注意事项\n第二条 内容。\n"
    result = parse_markdown(text)
    assert result.text.count("注意事项") == 2
    assert result.removed_lines == []


def test_code_block_exempt():
    # 代码块内的重复短行不参与统计、不被删除
    code_line = "return 0"
    text = (
        "说明文字。\n"
        "```python\n"
        f"{code_line}\n"
        f"{code_line}\n"
        f"{code_line}\n"
        "```\n"
        "结尾。\n"
    )
    result = parse_markdown(text)
    assert result.text.count(code_line) == 3
    assert result.removed_lines == []


def test_heading_kept_verbatim():
    text = "# 年假规定\n## 一、申请条件\n正文内容。\n"
    result = parse_markdown(text)
    assert "# 年假规定" in result.text
    assert "## 一、申请条件" in result.text


def test_blank_lines_normalized():
    text = "第一段。\n\n\n\n\n第二段。\n"
    result = parse_markdown(text)
    assert "\n\n\n" not in result.text
    assert "第一段。\n\n第二段。" in result.text
