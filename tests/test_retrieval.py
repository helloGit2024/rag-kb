"""向量检索集成测试（V-1）。

★ 依赖：PostgreSQL（含 pgvector）+ Ollama（bge-m3）必须在运行。
  这是集成测试，不是单元测试——检索质量必须用真实向量验证。

验收：提一个已知问题，Top-5 里包含答案所在块。
"""

from __future__ import annotations

import pytest

from app.core.embedding import embed_texts
from app.repos.chunk_repo import search_chunks


def _services_available() -> bool:
    """检查 PG 和嵌入服务是否可达（不可达则跳过测试）。"""
    try:
        embed_texts(["ping"])
    except Exception:
        return False
    try:
        search_chunks([0.0] * 1024, ["internal"], top_k=1)
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="PostgreSQL 或 Ollama(bge-m3) 未启动，跳过检索集成测试",
)


class TestRetrieval:
    def test_topk_contains_answer_chunk(self):
        """问题：工作满10年年假有多少天 → Top-5 应命中年假制度文档。"""
        question = "工作满10年年假有多少天"
        query_vec = embed_texts([question])[0]

        hits = search_chunks(query_vec, ["internal"], top_k=5)

        assert len(hits) > 0, "检索结果为空"
        # 答案在年假制度文档（doc_id=1）
        annual_leave_hits = [h for h in hits if h.doc_id == 1]
        assert len(annual_leave_hits) > 0, (
            f"Top-5 未命中年假制度文档(doc_id=1)，实际命中："
            f"{[(h.doc_id, h.score) for h in hits]}"
        )
        # 命中的块应包含答案关键词
        answer_hit = annual_leave_hits[0]
        assert "10年" in answer_hit.content or "10天" in answer_hit.content, (
            f"命中块不含答案关键词，内容前100字：{answer_hit.content[:100]}"
        )

    def test_permission_filter_in_sql(self):
        """scope_key 过滤应在 SQL 层生效：传不存在的 scope 应返回空。"""
        query_vec = embed_texts(["随便问个问题"])[0]

        hits = search_chunks(query_vec, ["nonexistent_scope"], top_k=5)

        assert hits == [], "传不存在的 scope_key 应返回空，证明权限过滤在 SQL 生效"

    def test_empty_scope_raises(self):
        """空 scope_key 列表应抛异常（R7：显式失败，不静默返回空）。"""
        with pytest.raises(ValueError, match="user_scope_keys 不能为空"):
            search_chunks([0.0] * 1024, [], top_k=5)
