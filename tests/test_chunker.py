"""分块器单元测试（V-1）。

验收重点：
1. 空文本 / 短文本 / 长文本的边界行为
2. 每块 ≤ chunk_size
3. 相邻块有 chunk_overlap 重叠
4. 优先按段落边界切，不在句子中间切断
5. char_start / char_end 能在原文精准定位
"""

from __future__ import annotations

import pytest

from app.services.chunker import chunk_text


class TestEdgeCases:
    def test_empty_text_returns_empty(self):
        assert chunk_text("") == []

    def test_short_text_single_chunk(self):
        text = "这是一段很短的文本。"
        chunks = chunk_text(text, chunk_size=512, chunk_overlap=64)
        assert len(chunks) == 1
        assert chunks[0].content == text
        assert chunks[0].chunk_index == 0
        assert chunks[0].char_start == 0
        assert chunks[0].char_end == len(text)

    def test_overlap_must_be_smaller_than_size(self):
        with pytest.raises(ValueError, match="chunk_overlap 必须小于 chunk_size"):
            chunk_text("随便写点什么", chunk_size=64, chunk_overlap=64)


class TestChunkSize:
    def test_each_chunk_not_exceed_size(self):
        text = "句子一。" * 300  # 1500 字
        chunks = chunk_text(text, chunk_size=100, chunk_overlap=20)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c.content) <= 100

    def test_overlap_between_adjacent_chunks(self):
        text = "句子一。" * 300
        chunks = chunk_text(text, chunk_size=100, chunk_overlap=20)
        for i in range(1, len(chunks)):
            prev = chunks[i - 1].content
            cur = chunks[i].content
            # 当前块的开头应该等于上一块末尾的重叠部分
            assert cur.startswith(prev[-20:])


class TestBoundaryPriority:
    def test_prefer_paragraph_boundary(self):
        """两个段落各 60 字，chunk_size=100：应按段落切，不合并。"""
        p1 = "第一段。" * 15  # 60 字
        p2 = "第二段。" * 15  # 60 字
        text = p1 + "\n\n" + p2
        chunks = chunk_text(text, chunk_size=100, chunk_overlap=0)
        # 应该切成 2 块，第一块是 p1+\n\n，第二块是 p2
        assert len(chunks) == 2
        assert chunks[0].content == p1 + "\n\n"
        assert chunks[1].content == p2

    def test_not_cut_in_middle_of_sentence(self):
        """当 chunk_size 是句长整数倍且 overlap=0 时，每块都应在句号结尾，
        证明分块器优先按句子边界切而非硬切。"""
        sentence = "这是一句话。"  # 6 字
        text = sentence * 50
        chunks = chunk_text(text, chunk_size=48, chunk_overlap=0)  # 48 = 6 * 8
        separators = {"\n", "。", "！", "？", "；", "，", "、", " "}
        for c in chunks:
            assert c.content[-1] in separators, (
                f"块在句子中间被切断：末尾字符='{c.content[-1]}'，内容={c.content!r}"
            )

    def test_prefers_boundary_over_hard_cut(self):
        """用变长句子（接近真实文档），overlap=0 时所有块都应在句号结尾，
        证明分块器优先按句子边界合并而非硬切。"""
        sentences = [
            "短。",
            "这是一句稍长的话。",
            "这是更长的一句话用来测试分块边界。",
            "非常短。",
            "再来一个中等长度的句子看看效果如何。",
        ]
        text = "".join(sentences * 20)
        chunks = chunk_text(text, chunk_size=60, chunk_overlap=0)
        separators = {"\n", "。", "！", "？", "；", "，", "、", " "}
        for c in chunks:
            assert c.content[-1] in separators, (
                f"块在句子中间被切断：末尾字符='{c.content[-1]}'，内容={c.content!r}"
            )


class TestCharPosition:
    def test_char_positions_match_original(self):
        text = "开头。" + "中间内容。" * 100 + "结尾。"
        chunks = chunk_text(text, chunk_size=80, chunk_overlap=16)
        for c in chunks:
            assert text[c.char_start : c.char_end] == c.content

    def test_char_positions_are_contiguous_with_overlap(self):
        text = "测试文本。" * 200
        chunks = chunk_text(text, chunk_size=100, chunk_overlap=20)
        for i in range(1, len(chunks)):
            # 后一块的 start 应该在前一块 end 之前（有重叠）
            assert chunks[i].char_start < chunks[i - 1].char_end
            # 重叠长度 = prev.end - cur.start
            overlap = chunks[i - 1].char_end - chunks[i].char_start
            assert overlap == 20
