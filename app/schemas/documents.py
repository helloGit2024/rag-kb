"""文档相关契约模型（V0.1 · T18 上传 / T22 列表）。

字段名以 docs/openapi.yaml 为准，不自创。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class UploadAccepted(BaseModel):
    """POST /api/v1/documents 的 202 响应（openapi.yaml L296—306）。

    doc_id 此时必定为 None——documents 行由 worker 异步创建（T13 编排），
    上传接口只建 parse_tasks 行。契约里 doc_id 不是 required，允许缺省。
    """

    task_id: int
    doc_id: int | None = None
    status: str


class DocumentOut(BaseModel):
    """GET /api/v1/documents 的 item（openapi.yaml Document schema，L1344—1362）。

    契约里没有的字段不出现——`id` / `kb_id` 在契约中都不是 nullable，
    但 dev 库里确实存在 kb_id 为空的存量行，故 kb_id 允许 None（否则序列化会炸）。
    `uploaded_by` 是 string（用户名），不是 id。
    """

    id: int
    kb_id: int | None
    title: str
    source_type: str
    file_size: int | None
    status: str
    scope: str
    chunk_count: int
    error_reason: str | None
    uploaded_by: str | None
    created_at: datetime


class DocumentListOut(BaseModel):
    """GET /api/v1/documents 的响应信封（openapi.yaml L263—274）。

    ★ 只有 items / total——与知识库列表不同，契约里文档列表**没有**
      page / page_size 字段（见 openapi.yaml L185—190 vs L270—274）。
      按契约实现，不擅自补字段。
    """

    items: list[DocumentOut]
    total: int
