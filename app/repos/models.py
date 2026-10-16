"""ORM 模型定义（V-1 骨架 → V0.1 · T14）。

表：documents / chunks（V-1骨架）+ users / user_scope_grants（V0.1 T14）。
★ app/repos/ 是唯一允许出现 SQL 的层；这里只放表结构元数据，不写查询 SQL。

FK 取舍（R8：先加字段、后加约束）：
- documents.uploaded_by → users.id：V-1 已加字段（可空），T14 补 FK 约束。
- documents.kb_id：等 T21 建 knowledge_bases 后再补 FK。
- chunks.doc_id → documents.id（V-1 已建）。
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
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
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
    # T14 补 FK → users.id（V-1 已有字段，R8 的"先字段、后约束"第二步）
    uploaded_by: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )
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


class User(Base):
    """用户表（V0.1 · T14）。字段见 docs/data-model.md 2.1。

    role 列现在就有、但 V0.1 不做 RBAC 判定（V1 才启用）；
    本版权限只用到 dept（本部门自动可见）与 user_scope_grants。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    dept: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
    )


class UserScopeGrant(Base):
    """授权表（V0.1 · T14）。字段见 docs/data-model.md 2.5。

    ★ 用授权表而不是在 users 上放单个部门字段：一个人可能同时属于多个范围
      （本部门 + 跨部门项目组 + 公开），统一用 scope_key 字符串抽象更易扩展。
    """

    __tablename__ = "user_scope_grants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=False
    )
    scope_key: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )

    __table_args__ = (
        # 权限解析要按 user_id 取授权列表，建索引免得每次全表扫
        Index("ix_user_scope_grants_user_id", "user_id"),
    )


# 任务状态取值（docs/data-model.md 2.6）
TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_DONE = "done"
TASK_STATUS_FAILED = "failed"

# 任务最大重试次数（docs/data-model.md 2.6「retry_count 最多 3」）。
# ★ 语义：失败后 retry_count += 1，只要 < 本值就重新入队再试一次；
#   达到本值即转终态 failed —— 最多重试 3 次，总尝试最多 4 次。
TASK_MAX_RETRY = 3

# 任务类型（data-model 2.6 枚举：parse / embed / eval）。
# V0.1 只用 parse——"解析＋向量化"是一条不可拆的链路（ingest_document），
# 拆成 parse/embed 两个任务只会增加重试与补偿难度；用 parse 指代整条链。
TASK_TYPE_PARSE = "parse"


class ParseTask(Base):
    """异步任务表（V0.1 · T18）。字段见 docs/data-model.md 2.6。

    ★ doc_id 可空：上传接口只建任务行（文件在 MinIO 暂存），
      worker 跑完 ingest 才回写 doc_id。
    ★ payload 存任务参数快照（kb_id / filename / scope / staging_key /
      file_md5），便于重放——Redis 队列消息里只放 task_id。
    """

    __tablename__ = "parse_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    doc_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=TASK_STATUS_PENDING
    )
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # worker 拉任务与状态查询接口都按 status 过滤
        Index("ix_parse_tasks_status", "status"),
    )
