"""V0.1 T22b：documents 同 kb 活跃行 (kb_id, file_md5) 部分唯一索引

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19

★ 为什么现在才加：T22b 修复同文件重复上传竞态。上传端查重（409）查的是
  "已入库的 documents 行"，但 documents 行由 worker 异步创建——两个任务
  并发时，worker 侧查重可能同时通过、同时插入，最终入库两行。
  DB 部分唯一索引是兜底：即使应用层全部失守，数据库也绝不允许同 kb 下
  同一 md5 出现两行 is_active=true 的文档。

★ 为什么是部分索引而不是普通唯一约束：is_active=false 的软删文档
  不应阻止重新上传同一文件（data-model 2.3 的 is_active 语义）。

★ R8 说明：本次只加索引、不改字段、不删数据，属于"先加约束"的
  第一步（且 kb_id / file_md5 字段早已存在，无需先加字段）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_documents_kb_md5_active",
        "documents",
        ["kb_id", "file_md5"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )


def downgrade() -> None:
    op.drop_index("uq_documents_kb_md5_active", table_name="documents")
