"""V-1 初始迁移：创建 documents / chunks 表

Revision ID: 0001
Revises:
Create Date: 2026-10-13

★ V-1 取舍：
- documents.kb_id / uploaded_by 不加 FK（关联表 users/knowledge_bases 在 V0.1 才建）
- chunks.doc_id 加 FK → documents.id
- chunks 含 scope_key（权限过滤）+ embedding vector(1024)（向量检索）+ tsv 生成列
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 启用 pgvector 扩展（镜像已安装扩展文件，需在库内启用）
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kb_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("parsed_key", sa.Text(), nullable=True),
        sa.Column("file_md5", sa.Text(), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column(
            "scope", sa.Text(), nullable=False, server_default=sa.text("'internal'")
        ),
        sa.Column("uploaded_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("doc_id", sa.BigInteger(), nullable=False),
        sa.Column("kb_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("scope_key", sa.Text(), nullable=False),
        # 全文检索字段，GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
        sa.Column(
            "tsv",
            postgresql.TSVECTOR,
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["doc_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # 向量检索（HNSW，余弦距离）
    op.create_index(
        "idx_chunks_embedding",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    # 全文检索
    op.create_index(
        "idx_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin"
    )
    # ★ 权限过滤 + 检索的核心复合索引
    op.create_index("idx_chunks_scope_kb", "chunks", ["scope_key", "kb_id"])
    # 去重与清理
    op.create_index(
        "idx_chunks_doc_idx", "chunks", ["doc_id", "chunk_index"], unique=True
    )


def downgrade() -> None:
    op.drop_index("idx_chunks_doc_idx", table_name="chunks")
    op.drop_index("idx_chunks_scope_kb", table_name="chunks")
    op.drop_index("idx_chunks_tsv", table_name="chunks")
    op.drop_index("idx_chunks_embedding", table_name="chunks")
    op.drop_table("chunks")
    op.drop_table("documents")
