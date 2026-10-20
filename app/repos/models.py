"""ORM 模型定义（V-1 骨架 → V0.1 · T14 / T18 / T21）。

表：documents / chunks（V-1骨架）+ users / user_scope_grants（V0.1 T14）
    + parse_tasks（V0.1 T18）+ knowledge_bases（V0.1 T21）
    + eval_datasets / eval_runs / eval_run_items（V0.1 T33）。
★ app/repos/ 是唯一允许出现 SQL 的层；这里只放表结构元数据，不写查询 SQL。

FK 取舍（R8：先加字段、后加约束）：
- documents.uploaded_by → users.id：V-1 已加字段（可空），T14 补 FK 约束。
- documents.kb_id → knowledge_bases.id：**仍未补**。T21 建了表，但 dev 库已有
  kb_id=1/2/3 的文档行而无对应知识库行，直接加 FK 会立刻违反约束。
  补 FK 需先补齐知识库数据，单独排任务（见 alembic/versions/0004 注释）。
- chunks.doc_id → documents.id（V-1 已建）。
- knowledge_bases.created_by → users.id（T21 新建表时一并加，无历史数据）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
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
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    """带时区的 UTC 当前时间（T21）。

    为什么新表不再用 `datetime.utcnow()`：它返回**朴素** datetime
    （无 tzinfo），写进 `timestamptz` 列后读回来没有时区，
    JSON 序列化就成了 `2026-10-16T06:57:29.459297`（无 Z）——
    与列表接口里读库得到的 `...Z`（带时区）**同一字段两种形状**。
    契约里 created_at 是 date-time（应带时区），故新表直接用 aware 值。
    ★ 不动 documents/chunks/users 的老默认值：那是既有行为，
      统一替换属独立重构（R9 改动最小化）。
    """
    return datetime.now(UTC)


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

    __table_args__ = (
        # T22b：同 kb 内活跃文档的 (kb_id, file_md5) 唯一索引，
        # 防止并发 worker 同时插入两行重复文档（竞态兜底）
        Index(
            "uq_documents_kb_md5_active",
            "kb_id",
            "file_md5",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
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


class KnowledgeBase(Base):
    """知识库表（V0.1 · T21）。字段见 docs/data-model.md 2.2。

    ★ default_scope 是"新文档默认可见范围"（spec.md Q-01，本项目定为 internal）：
      上传时不显式传 scope 就取本值。它只做默认值，不做权限判定
      （判定仍在 chunks.scope_key + kb_id 两条件 SQL 里，R2）。
    ★ strategy_id 本版只留列不启用（V1 绑 RAG 策略，data-model 2.2 标注 V1）。
    """

    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    default_scope: Mapped[str] = mapped_column(
        Text, nullable=False, default="internal"
    )
    strategy_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_knowledge_bases_name"),
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


# ── 评估三张表（V0.1 · T33）────────────────────────────────────────────
# 字段与设计理由逐条见 docs/data-model.md 2.11—2.13，这里只映射、不自创。
# 两条纪律由 DB 约束兜底（见 alembic/versions/0006 头注释）：
# ck_active_requires_confirmed（R13/R16）、ck_attribution（R15）。


class EvalDataset(Base):
    """数据集元数据与校验和（V0.1 · T33，data-model 2.11）。

    ★ 题目本体不进表：这里只存元数据 + sha256（checksum）；
      item_id 在 eval_run_items 里因此是 text 而非外键。
    """

    __tablename__ = "eval_datasets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    repo_path: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    file_format: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    distribution: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    tag_stats: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    unconfirmed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_dataset_name_ver"),
    )


class EvalRun(Base):
    """一次评估的运行记录（V0.1 · T33，data-model 2.12）。

    ★ git_commit + config_snapshot 是"两次数字可不可比"的唯一依据（R14）。
    ★ 同一数据集只允许一个基线：由部分唯一索引 uq_run_baseline 兜底，
      ORM 层不重复实现（Index + postgresql_where）。
    """

    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("eval_datasets.id"), nullable=False
    )
    git_commit: Mapped[str] = mapped_column(Text, nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model_config_ids: Mapped[Optional[List[int]]] = mapped_column(
        ARRAY(BigInteger), nullable=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    metrics: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    attribution_counts: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    cost_cny: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    total_tokens_in: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    total_tokens_out: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    wall_time_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    report_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_baseline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    baseline_of: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    error_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_run_baseline",
            "dataset_id",
            unique=True,
            postgresql_where=text("is_baseline = true"),
        ),
    )


class EvalRunItem(Base):
    """逐条评估结果（V0.1 · T33，data-model 2.13）。

    ★ answer 必须存：系统实际输出不留档就无法复盘。
    ★ attribution_confirmed 只能人工写入（R15）；ck_attribution 约束
      保证"结论必须与 candidate + confirmed_by 并存"，ORM 不另设默认。
    """

    __tablename__ = "eval_run_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("eval_runs.id"), nullable=False
    )
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    scores: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    verdict: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retrieval_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    retrieved_chunk_ids: Mapped[Optional[List[int]]] = mapped_column(
        ARRAY(BigInteger), nullable=True
    )
    answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refused: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    degraded: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retrieve_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    generate_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_cny: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), nullable=True)
    attribution_candidate: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attribution_confirmed: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("uq_run_item", "run_id", "item_id", unique=True),
    )
