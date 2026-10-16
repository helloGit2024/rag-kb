"""知识库 CRUD 路由（V0.1 · T21）。

4 个端点，路径与字段严格按 docs/openapi.yaml：
  GET    /api/v1/knowledge-bases        列表（分页）
  POST   /api/v1/knowledge-bases        创建 → 201
  PATCH  /api/v1/knowledge-bases/{id}   修改 → 200
  DELETE /api/v1/knowledge-bases/{id}   删除（级联）→ 204

本层只做：解析入参 → 调 service → 映射响应。不写业务逻辑、不查库（目录约定）。

★ 身份：四个端点都挂 `_require_user_permission`（与 query / documents 一致）。
  按 T21 定档，列表**不按权限过滤**（知识库只是容器），但身份仍必须合法——
  security.md 2.2 要求任何接口进业务逻辑前完成身份解析。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.v1.query import _require_user_permission
from app.repos.db import get_session
from app.schemas.kb import KbCreateIn, KbListOut, KbUpdateIn, KnowledgeBaseOut
from app.services import kb as kb_service
from app.services.kb import KbSummary
from app.services.permission import PermissionBundle

router = APIRouter(prefix="/api/v1", tags=["kb"])


def _to_out(summary: KbSummary) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=summary.id,
        name=summary.name,
        description=summary.description,
        default_scope=summary.default_scope,
        doc_count=summary.doc_count,
        created_at=summary.created_at,
    )


@router.get("/knowledge-bases", response_model=KbListOut)
def list_knowledge_bases(
    session: Annotated[Session, Depends(get_session)],
    perm: Annotated[PermissionBundle, Depends(_require_user_permission)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> KbListOut:
    """知识库列表（分页，含各自有效文档数）。

    排序：id 倒序（新建的在前）——契约未规定顺序，取最直观的默认。
    """
    _ = perm  # 身份合法性由依赖保证；V0.1 不按权限过滤列表（见服务层注释）
    items, total = kb_service.list_kbs(session, page=page, page_size=page_size)
    return KbListOut(
        items=[_to_out(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/knowledge-bases", response_model=KnowledgeBaseOut, status_code=201)
def create_knowledge_base(
    req: KbCreateIn,
    session: Annotated[Session, Depends(get_session)],
    perm: Annotated[PermissionBundle, Depends(_require_user_permission)],
) -> KnowledgeBaseOut:
    """创建知识库。

    Raises:
        KbError: 400（名称/描述/scope 非法）、409 DUPLICATE_KB_NAME。
    """
    summary = kb_service.create_kb(
        session,
        name=req.name,
        description=req.description,
        default_scope=req.default_scope,
        created_by=perm.user_id,
    )
    return _to_out(summary)


@router.patch("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseOut)
def update_knowledge_base(
    kb_id: int,
    req: KbUpdateIn,
    session: Annotated[Session, Depends(get_session)],
    perm: Annotated[PermissionBundle, Depends(_require_user_permission)],
) -> KnowledgeBaseOut:
    """修改知识库（局部更新，未传的字段不改）。

    Raises:
        KbError: 404 NOT_FOUND、400（入参非法）、409 DUPLICATE_KB_NAME。
    """
    _ = perm
    summary = kb_service.update_kb(
        session,
        kb_id,
        name=req.name,
        description=req.description,
        default_scope=req.default_scope,
    )
    return _to_out(summary)


@router.delete("/knowledge-bases/{kb_id}", status_code=204)
def delete_knowledge_base(
    kb_id: int,
    session: Annotated[Session, Depends(get_session)],
    perm: Annotated[PermissionBundle, Depends(_require_user_permission)],
) -> Response:
    """删除知识库（级联：清对象存储 → 删向量 → 软删文档 → 删库行）。

    ★ 可见范围（OPEN-QUESTIONS #8 统一，与 DELETE /documents 同口径）：
      按 KB 的 `default_scope` + kb 授权两轴校验（service → repo 下沉到
      SQL WHERE，R2）；不在范围内返回 404（不是 403）。

    Raises:
        KbError: 404 NOT_FOUND（不存在，或不在可见范围内）。
    """
    kb_service.delete_kb(
        session, kb_id, scope_keys=perm.scope_keys, kb_ids=perm.kb_ids
    )
    return Response(status_code=204)
