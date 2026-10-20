"""V0.1 T33：评估结果入库——eval_datasets / eval_runs / eval_run_items

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20

★ 为什么 V0.1 就建这三张表（而不是等 V1 评估平台）：
  没有 eval_runs，"这次数字是跟谁比出来的基线"只存在于人的记忆里；
  没有 eval_run_items，汇总指标掉了无法回答"哪几条从通过变失败"。
  表结构与设计理由逐字段见 docs/data-model.md 2.11—2.13，本迁移严格照其落地，
  一个字段都不自创。

★ 两条纪律由数据库约束兜底（不靠口头自觉）：
  - ck_active_requires_confirmed：标准答案未全部确认（unconfirmed_count>0）的
    数据集禁止置 active —— "标准答案必须人工确认"从约定变成约束（R13/R16）。
  - ck_attribution：归因结论 attribution_confirmed 必须与 candidate +
    confirmed_by 并存 —— "归因结论只能人工写入且必须带依据"（R15）。

★ 题目本体不进表：eval_datasets 只存元数据与 sha256（checksum），
  题目文件归 evals/（Git）+ MinIO 归档快照；eval_run_items.item_id 因此
  是 text 而非外键（data-model 2.11 开头说明）。

★ R8 说明：本次纯新增表，不动任何既有表结构与数据，downgrade 反序 drop 即可回滚。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 2.11 eval_datasets：数据集元数据与校验和（题目本体不进表）──
    op.create_table(
        "eval_datasets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("version", sa.Text, nullable=False),
        sa.Column("repo_path", sa.Text, nullable=False),
        sa.Column("file_path", sa.Text, nullable=True),
        sa.Column("file_format", sa.Text, nullable=False),
        sa.Column("checksum", sa.Text, nullable=False),
        sa.Column("item_count", sa.Integer, nullable=False),
        sa.Column("distribution", postgresql.JSONB, nullable=True),
        sa.Column("tag_stats", postgresql.JSONB, nullable=True),
        sa.Column("state", sa.Text, nullable=False, server_default="draft"),
        sa.Column("unconfirmed_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("source", sa.Text, nullable=False, server_default="manual"),
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
        sa.UniqueConstraint("name", "version", name="uq_dataset_name_ver"),
        # 只要还有未确认条目，就不允许启用（R13/R16 的 DB 兜底）
        sa.CheckConstraint(
            "state <> 'active' OR unconfirmed_count = 0",
            name="ck_active_requires_confirmed",
        ),
    )

    # ── 2.12 eval_runs：每次评估的运行记录（可复现的地基，R14）──
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "dataset_id",
            sa.BigInteger,
            sa.ForeignKey("eval_datasets.id"),
            nullable=False,
        ),
        # 没有 git_commit + config_snapshot，两次评估的数字不可比
        sa.Column("git_commit", sa.Text, nullable=False),
        sa.Column("config_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("model_config_ids", postgresql.ARRAY(sa.BigInteger), nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("metrics", postgresql.JSONB, nullable=True),
        sa.Column("attribution_counts", postgresql.JSONB, nullable=True),
        sa.Column("cost_cny", sa.Numeric(10, 4), nullable=True),
        sa.Column("total_tokens_in", sa.BigInteger, nullable=True),
        sa.Column("total_tokens_out", sa.BigInteger, nullable=True),
        sa.Column("wall_time_s", sa.Integer, nullable=True),
        sa.Column("report_path", sa.Text, nullable=True),
        sa.Column(
            "is_baseline",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("baseline_of", sa.BigInteger, nullable=True),
        sa.Column("triggered_by", sa.Text, nullable=False, server_default="manual"),
        sa.Column("error_reason", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 同一数据集下只允许一个基线（部分唯一索引）
    op.create_index(
        "uq_run_baseline",
        "eval_runs",
        ["dataset_id"],
        unique=True,
        postgresql_where=sa.text("is_baseline = true"),
    )

    # ── 2.13 eval_run_items：逐条结果（回归定位与归因的载体）──
    op.create_table(
        "eval_run_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "run_id", sa.BigInteger, sa.ForeignKey("eval_runs.id"), nullable=False
        ),
        # 题目在数据集文件里的 id（如 Q001）；不是外键——题目本体在文件里
        sa.Column("item_id", sa.Text, nullable=False),
        # category/question 冗余存储：报告可独立阅读、按类聚合不回读文件
        sa.Column("category", sa.Text, nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("scores", postgresql.JSONB, nullable=True),
        sa.Column("verdict", sa.Text, nullable=True),
        # 只跑检索时标准来源是否出现在 Top-5——归因的关键布尔值
        sa.Column("retrieval_hit", sa.Boolean, nullable=True),
        sa.Column(
            "retrieved_chunk_ids", postgresql.ARRAY(sa.BigInteger), nullable=True
        ),
        # 系统实际输出必须存，否则无法复盘
        sa.Column("answer", sa.Text, nullable=True),
        sa.Column("refused", sa.Boolean, nullable=True),
        sa.Column("degraded", sa.Boolean, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("retrieve_ms", sa.Integer, nullable=True),
        sa.Column("generate_ms", sa.Integer, nullable=True),
        sa.Column("cost_cny", sa.Numeric(10, 4), nullable=True),
        sa.Column("attribution_candidate", sa.Text, nullable=True),
        # 归因结论只能人工写入（R15）；未确认的不计入任何统计
        sa.Column("attribution_confirmed", sa.Text, nullable=True),
        sa.Column("confirmed_by", sa.Text, nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        # 归因结论必须与依据并存：不能只有结论、没有依据（R15 的 DB 兜底）
        sa.CheckConstraint(
            "attribution_confirmed IS NULL "
            "OR (attribution_candidate IS NOT NULL AND confirmed_by IS NOT NULL)",
            name="ck_attribution",
        ),
    )
    op.create_index("uq_run_item", "eval_run_items", ["run_id", "item_id"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_run_item", table_name="eval_run_items")
    op.drop_table("eval_run_items")
    op.drop_index("uq_run_baseline", table_name="eval_runs")
    op.drop_table("eval_runs")
    op.drop_table("eval_datasets")
