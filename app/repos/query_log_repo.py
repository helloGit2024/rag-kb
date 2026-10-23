"""问答日志落表（V0.1 · T34，data-model 2.9）。

职责：把 `_log_qa` 收集到的一次问答写进 `query_logs`。

★ 为什么这里自己开一个短事务，而不是复用请求的 Session：
  ① `get_session()` 是 FastAPI 依赖，服务层拿不到它；
  ② 即使传进来也不安全——PostgreSQL 事务一旦被某条 SQL 置为 aborted，
     后续所有语句（包括检索）都会失败；日志写失败不能污染主链路。
  独立短事务的好处：失败只影响这一行，问答照常返回。

★ R1：连接串只从 `get_engine()` 取（内部读 DATABASE_URL），本文件不碰 DSN。
★ R7：失败不吞——调用方（qa._log_qa）负责显式打 stderr。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.repos.db import get_engine
from app.repos.models import QueryLog


def insert_log(
    *,
    user_id: int | None,
    kb_id: int | None,
    question: str,
    scope_snapshot: dict,
    retrieved_chunk_ids: list[int],
    answer: str,
    refused: bool,
    citations: list[dict],
    latency_ms: int | None,
    retrieve_ms: int | None,
    generate_ms: int | None,
    cost_cny: float | None,
    model_used: str | None,
    rerank_ms: int | None = None,
    trace_id: str | None = None,
) -> int:
    """写一行问答日志，返回自增 id。

    Raises:
        Exception: 任何写库失败原样上抛，由调用方决定如何显式记录（R7）。
            本函数不做兜底，也不返回假 id。
    """
    row = QueryLog(
        user_id=user_id,
        kb_id=kb_id,
        question=question,
        scope_snapshot=scope_snapshot,
        retrieved_chunk_ids=list(retrieved_chunk_ids) or None,
        answer=answer,
        refused=refused,
        citations=citations,
        latency_ms=latency_ms,
        retrieve_ms=retrieve_ms,
        generate_ms=generate_ms,
        rerank_ms=rerank_ms,
        # float → Decimal 显式转换（Numeric(10,4)，与 eval 侧同一约定）
        cost_cny=(
            Decimal(str(round(cost_cny, 6))) if cost_cny is not None else None
        ),
        model_used=model_used,
        trace_id=trace_id,
    )
    with Session(get_engine()) as session:
        session.add(row)
        session.commit()
        return row.id
