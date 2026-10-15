"""文档相关契约模型（V0.1 · T18）。

字段名以 docs/openapi.yaml 为准，不自创。
"""

from __future__ import annotations

from pydantic import BaseModel


class UploadAccepted(BaseModel):
    """POST /api/v1/documents 的 202 响应（openapi.yaml L296—306）。

    doc_id 此时必定为 None——documents 行由 worker 异步创建（T13 编排），
    上传接口只建 parse_tasks 行。契约里 doc_id 不是 required，允许缺省。
    """

    task_id: int
    doc_id: int | None = None
    status: str
