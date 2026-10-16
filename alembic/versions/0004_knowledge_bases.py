"""V0.1 T21：创建 knowledge_bases（知识库表）

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16

★ 字段定义逐字来自 docs/data-model.md 2.2。
★ 为什么现在才建：V-1 骨架里 documents.kb_id 只是一个无 FK 的裸字段
  （models.py 注明"等 T21 建 knowledge_bases 后再补 FK"）；T21 是知识库
  CRUD，必须先有表。
★ strategy_id 不加 FK：它指向 V1 才建的 model_configs（RAG 策略绑定），
  本版只留列、不启用（data-model 2.2 标注"V1"）。
★ ★ 不给 documents.kb_id 补 FK：dev 库里已有 kb_id=1/2/3 的文档行，
  而 knowledge_bases 是新建空表——直接加 FK 会立刻违反约束、迁移失败。
  补 FK 需先灌入对应知识库行（属数据治理，单独排任务，见 R8"先字段后约束"）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # 新文档默认可见范围（spec.md Q-01，默认 internal）
        sa.Column(
            "default_scope",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
        # V1：绑定的 RAG 策略（model_configs 表 V1 才建，此处只留列）
        sa.Column("strategy_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id"),
        # data-model 2.2：name UNIQUE —— 重名由数据库兜底（返回 409 DUPLICATE_KB_NAME）
        sa.UniqueConstraint("name", name="uq_knowledge_bases_name"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_knowledge_bases_created_by",
        ),
    )


def downgrade() -> None:
    op.drop_table("knowledge_bases")
