"""V0.1 T34：问答流水入库——query_logs

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-23

★ 为什么 V0.1 就落库（而不是只看 stderr 日志）：
  query_logs.scope_snapshot 是排查越权问题的唯一手段——用户权限会变，
  事后复现时只有它能回答"当时他到底能看到什么"。stderr 日志会被轮转丢掉。
★ 字段严格按 docs/data-model.md 2.9 落地，一个都不自创。
  kb_id 单列只承载"恰好一个知识库"的场景；多库查询时完整列表进 scope_snapshot
  （两轴独立，kb 维度不折进 scope_keys，见 app/services/permission.py 模块说明）。
★ rerank_ms / trace_id 在 V0.1 恒为 NULL（不做重排、无链路追踪），列先建好，
  V1 接上时无需再改表（R8：能一次加完就不留二次迁移）。
★ R8 说明：本次纯新增表，不动任何既有表结构与数据，downgrade 直接 drop 即可回滚。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 2.9 query_logs：每次问答一行（越权排查的唯一手段）──
    op.create_table(
        "query_logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        # users.id 暂不建外键：V0.1 的身份方案是 header 用户名，用户可能被删；
        # 流水记录不应因用户删除而写不进去（data-model 2.9 未声明 FK）
        sa.Column("user_id", sa.BigInteger, nullable=True),
        # 只在"恰好一个知识库"时有值；多库时为 NULL，完整列表见 scope_snapshot
        sa.Column("kb_id", sa.BigInteger, nullable=True),
        sa.Column("question", sa.Text, nullable=False),
        # ★ 当时生效的权限范围（两轴）：{"scope_keys": [...], "kb_ids": [...]}
        sa.Column("scope_snapshot", postgresql.JSONB, nullable=False),
        sa.Column(
            "retrieved_chunk_ids", postgresql.ARRAY(sa.BigInteger), nullable=True
        ),
        sa.Column("answer", sa.Text, nullable=False),
        sa.Column("refused", sa.Boolean, nullable=False),
        sa.Column("citations", postgresql.JSONB, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("retrieve_ms", sa.Integer, nullable=True),
        sa.Column("generate_ms", sa.Integer, nullable=True),
        # V0.1 不做重排 → 恒 NULL；列先建好，V1 无需改表
        sa.Column("rerank_ms", sa.Integer, nullable=True),
        sa.Column("cost_cny", sa.Numeric(10, 4), nullable=True),
        sa.Column("model_used", sa.Text, nullable=True),
        # V0.1 无链路追踪 → 恒 NULL；V1 接 Langfuse 时直接写
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("query_logs")
