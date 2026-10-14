"""ORM 模型定义（V-1）。

只定义 documents / chunks 两张表。
★ app/repos/ 是唯一允许出现 SQL 的层；这里只放表结构元数据，不写查询 SQL。

V-1 取舍：
- documents.kb_id / uploaded_by 暂不加 FK（关联表 users/knowledge_bases 在 V0.1 才建），
  等 V0.1 建完关联表后用迁移补 FK（符合 R8：先加字段、后加约束）。
- chunks.doc_id 加 FK → documents.id。
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # V-1 不加 FK，V0.1 补 → knowledge_bases.id
    kb_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parsed_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_md5: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    error_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="internal")
    # V-1 不加 FK，V0.1 补 → users.id
    uploaded_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class Chunk(Base):
    """★ 最关键的表：权限过滤字段 scope_key 与向量 embedding 同在一行，
    检索与权限过滤在同一条 SQL 里完成（AGENTS.md R2）。"""

    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id"), nullable=False
    )
    # kb_id 冗余存储，避免检索时 JOIN
    kb_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    char_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    embedding: Mapped[Optional[List[float]]] = mapped_column(
        Vector(1024), nullable=True
    )
    # ★ 权限过滤字段，值由 doc.scope 推导，形如 dept:销售部 / internal / public
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    # 全文检索字段，GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
    tsv = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', content)", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        # 向量检索（HNSW，余弦距离）
        Index(
            "idx_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # 全文检索（V1 启用）
        Index("idx_chunks_tsv", "tsv", postgresql_using="gin"),
        # ★ 权限过滤 + 检索的核心复合索引
        Index("idx_chunks_scope_kb", "scope_key", "kb_id"),
        # 去重与清理
        Index("idx_chunks_doc_idx", "doc_id", "chunk_index", unique=True),
    )
