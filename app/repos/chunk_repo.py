"""chunks 表数据访问：检索（V-1）与批量写入（V0.1 · T13）。

★ 权限过滤必须下沉到 SQL 的 WHERE 子句（AGENTS.md R2）：
  禁止"先查 Top-20 再在 Python 里筛掉无权访问的"——越权内容会进内存，
  且筛完候选太少会导致召回崩塌。本函数的 scope_key 条件直接写在 SQL 里。

★ V-1 取舍：
  scope_keys 传固定值 ["internal"]，不做真实权限隔离（真实隔离在 V0.1 T15 启用）。
  但函数签名与 SQL 结构已就绪，后续只需把真实 scope_key 传进来即可。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.repos.db import get_engine
from app.repos.models import Chunk


@dataclass
class ChunkHit:
    """检索命中的一个块。"""

    chunk_id: int
    content: str
    doc_id: int
    doc_title: str
    char_start: int
    char_end: int
    score: float


def search_chunks(
    query_vec: list[float],
    scope_keys: list[str],
    top_k: int = 5,
    *,
    kb_ids: list[int] | None = None,
    engine: Engine | None = None,
) -> list[ChunkHit]:
    """向量检索 Top-K。

    Args:
        query_vec: 查询文本的嵌入向量（1024 维）。
        scope_keys: 当前用户有权访问的 scope_key 列表（由 permission 解析得到）。
            ★ 权限过滤在 SQL WHERE 里完成，不在应用层过滤。
        top_k: 返回条数。
        kb_ids: 当前用户有权访问的 kb_id 列表（T17b 新增）。非空时追加
            `AND c.kb_id = ANY(:kb_ids)` 条件；空时不追加（向后兼容）。
            ★ 不传或传 [] 都会让 kb 条件不生效——多库场景下这意味着"所有 kb 可见"，
              行为取决于 chunks 表里的数据分布；caller 应显式传入以保证隔离。
        engine: 可选，注入的 SQLAlchemy Engine（测试时用）。不传则用 db.get_engine()。

    Returns:
        按相关度降序的命中列表，score = 1 - 余弦距离（越大越相关）。
    """
    if not scope_keys:
        # R7：显式失败，不返回空列表藏问题
        raise ValueError("scope_keys 不能为空，否则会检索出越权内容")

    eng = engine or get_engine()
    SessionLocal = sessionmaker(bind=eng)

    # WHERE 条件片段：scope_key 必选，kb_id 可选（T17b）
    where_clauses = ["c.scope_key = ANY(:scope_keys)"]
    params: dict[str, object] = {
        "query_vec": query_vec,
        "scope_keys": list(scope_keys),
        "top_k": top_k,
    }
    if kb_ids:
        where_clauses.append("c.kb_id = ANY(:kb_ids)")
        params["kb_ids"] = list(kb_ids)

    sql = text(
        f"""
        SELECT c.id, c.content, c.doc_id, d.title, c.char_start, c.char_end,
               1 - (c.embedding <=> CAST(:query_vec AS vector)) AS score
        FROM chunks c
        JOIN documents d ON d.id = c.doc_id
        WHERE {" AND ".join(where_clauses)}
        ORDER BY c.embedding <=> CAST(:query_vec AS vector)
        LIMIT :top_k
        """
    )

    with SessionLocal() as session:
        rows = session.execute(sql, params).fetchall()

    return [
        ChunkHit(
            chunk_id=r[0],
            content=r[1],
            doc_id=r[2],
            doc_title=r[3],
            char_start=r[4],
            char_end=r[5],
            score=float(r[6]),
        )
        for r in rows
    ]


@dataclass(frozen=True)
class ChunkInsert:
    """待写入的一个块。

    doc_id / kb_id / scope_key 是整批共用的，不放这里——避免每行重复携带。
    """

    content: str
    chunk_index: int
    char_start: int
    char_end: int
    embedding: list[float]


def insert_chunks(
    session: Session,
    *,
    doc_id: int,
    kb_id: int,
    scope_key: str,
    items: Sequence[ChunkInsert],
) -> int:
    """批量插入 chunks，返回写入条数。

    ★ 不 commit（与 insert_document 同一约定）：事务边界由 ingest 编排控制。
    ★ scope_key 与 embedding 写进同一行：检索时的权限过滤才能在一条 SQL 里完成（R2）。
    """
    session.add_all(
        [
            Chunk(
                doc_id=doc_id,
                kb_id=kb_id,
                chunk_index=item.chunk_index,
                content=item.content,
                char_start=item.char_start,
                char_end=item.char_end,
                embedding=item.embedding,
                scope_key=scope_key,
            )
            for item in items
        ]
    )
    session.flush()
    return len(items)
