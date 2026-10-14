"""V0.1 T14：创建 users / user_scope_grants，补 documents.uploaded_by 外键

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-14

★ 字段定义逐字来自 docs/data-model.md 2.1 / 2.5。
★ R8（先字段、后约束）：documents.uploaded_by 字段 V-1 已建（可空），
  本次只补 FK 约束，不同时改字段。
★ knowledge_bases 表属 T21，本次不建 → documents.kb_id 的 FK 也留到那时补。
★ scope_key 不折进 kb 可见性：data-model 第三节的正确 SQL 用
  `scope_key = ANY(...) AND kb_id = ANY(...)` 两个独立条件。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("dept", sa.Text(), nullable=True),
        sa.Column(
            "role", sa.Text(), nullable=False, server_default=sa.text("'user'")
        ),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    op.create_table(
        "user_scope_grants",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("granted_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_scope_grants_user_id", "user_scope_grants", ["user_id"])

    # R8 第二步：字段已在，这里只补约束。存量行该列为 NULL，加 FK 安全。
    op.create_foreign_key(
        "fk_documents_uploaded_by_users",
        "documents",
        "users",
        ["uploaded_by"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_documents_uploaded_by_users", "documents", type_="foreignkey"
    )
    op.drop_index("ix_user_scope_grants_user_id", table_name="user_scope_grants")
    op.drop_table("user_scope_grants")
    op.drop_table("users")
