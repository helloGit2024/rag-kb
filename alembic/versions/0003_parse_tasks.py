"""V0.1 T18：创建 parse_tasks（异步任务表）

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15

★ 字段定义逐字来自 docs/data-model.md 2.6。
★ doc_id 可空：上传接口只建任务行（文件在 MinIO 暂存），
  worker 跑完 ingest 才回写 doc_id —— 建表时不加 FK，T19/T22 再补约束（R8）。
★ payload jsonb 存任务参数快照（kb_id / filename / scope / staging_key / file_md5），
  便于重放与排查，不依赖 Redis 里的消息内容。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "parse_tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("doc_id", sa.BigInteger(), nullable=True),
        sa.Column("task_type", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column(
            "progress", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    # worker 拉任务 / 状态查询接口都按 status 过滤
    op.create_index("ix_parse_tasks_status", "parse_tasks", ["status"])


def downgrade() -> None:
    op.drop_index("ix_parse_tasks_status", table_name="parse_tasks")
    op.drop_table("parse_tasks")
