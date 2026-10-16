"""知识库接口的请求/响应模型（V0.1 · T21）。

字段与约束逐条对齐 docs/openapi.yaml 的 /api/v1/knowledge-bases：
- POST body: name（必填，≤100）、description（≤500）、default_scope（默认 internal）
- PATCH body: 三个字段都可选
- 响应：KnowledgeBase（id/name/description/default_scope/doc_count/created_at）
- 列表响应：items/total/page/page_size
★ 这层只放结构与轻量校验，不写业务逻辑（名称空白、scope 白名单在 services/kb.py）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class KbCreateIn(BaseModel):
    """POST /knowledge-bases 请求体。"""

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    # openapi：default: internal
    default_scope: str = "internal"


class KbUpdateIn(BaseModel):
    """PATCH /knowledge-bases/{id} 请求体（全部可选 = 局部更新）。"""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    default_scope: str | None = None


class KnowledgeBaseOut(BaseModel):
    """openapi components.schemas.KnowledgeBase。"""

    id: int
    name: str
    description: str | None
    default_scope: str
    doc_count: int
    created_at: datetime


class KbListOut(BaseModel):
    """GET /knowledge-bases 的分页信封。"""

    items: list[KnowledgeBaseOut]
    total: int
    page: int
    page_size: int
