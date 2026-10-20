"""文档相关路由：上传（V0.1 · T18）、列表与删除（V0.1 · T22）。

- POST   /api/v1/documents               上传（异步，202 + task_id）—— T18
- GET    /api/v1/documents               列表（分页，按权限过滤）—— T22
- DELETE /api/v1/documents/{doc_id}      删除（级联三步）      —— T22

上传层只做：收文件 → 同步校验（类型/大小/去重）→ 暂存 MinIO → 建任务行 →
入队 → 202 + task_id。解析与向量化全部异步（R10：HTTP 请求里禁止同步重活）。
列表 / 删除只做：解析入参 → 调 service → 映射响应，不写业务逻辑、不查库。

★ 任务状态查询属 T23，不在本文件。
"""

from __future__ import annotations

import uuid
from typing import Annotated

import redis
from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.orm import Session

from app.api.v1.query import ApiError, _require_user_permission
from app.core.queue import enqueue_ingest
from app.core.storage import ensure_bucket, put_bytes, staging_key
from app.repos.db import get_session
from app.repos.document_repo import DocumentRow
from app.repos.task_repo import insert_task, mark_failed
from app.schemas.documents import DocumentListOut, DocumentOut, UploadAccepted
from app.services import document as document_service
from app.services import kb as kb_service
from app.services.file_validator import validate_upload
from app.services.permission import PermissionBundle

router = APIRouter(prefix="/api/v1", tags=["documents"])


def _to_out(row: DocumentRow) -> DocumentOut:
    return DocumentOut(
        id=row.id,
        kb_id=row.kb_id,
        title=row.title,
        source_type=row.source_type,
        file_size=row.file_size,
        status=row.status,
        scope=row.scope,
        chunk_count=row.chunk_count,
        error_reason=row.error_reason,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
    )


@router.get("/documents", response_model=DocumentListOut)
def list_documents(
    session: Annotated[Session, Depends(get_session)],
    perm: Annotated[PermissionBundle, Depends(_require_user_permission)],
    kb_id: Annotated[int | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> DocumentListOut:
    """文档列表（分页，只含未软删文档）。

    ★ 权限：可见范围（scope_keys + kb_ids 两轴）传给 service，由 repo 写进
      SQL WHERE（R2）——**不在应用层筛**。文档标题常含敏感信息，
      不做过滤等于列表接口泄密。

    Raises:
        DocumentError: 400（status 非法）。
    """
    rows, total = document_service.list_documents(
        session,
        scope_keys=perm.scope_keys,
        kb_ids=perm.kb_ids,
        kb_id=kb_id,
        status=status,
        page=page,
        page_size=page_size,
    )
    return DocumentListOut(items=[_to_out(r) for r in rows], total=total)


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(
    doc_id: int,
    session: Annotated[Session, Depends(get_session)],
    perm: Annotated[PermissionBundle, Depends(_require_user_permission)],
) -> Response:
    """删除文档（级联：删对象存储 → 删向量 → 软删库行）。

    Raises:
        DocumentError: 404 NOT_FOUND（不存在、已软删，或不在可见范围内）。
    """
    document_service.delete_document(
        session,
        doc_id=doc_id,
        scope_keys=perm.scope_keys,
        kb_ids=perm.kb_ids,
    )
    return Response(status_code=204)


@router.post(
    "/documents",
    response_model=UploadAccepted,
    status_code=202,
)
def upload_document(
    file: Annotated[UploadFile, File(description="最大 20 MB，仅支持 .txt / .md")],
    kb_id: Annotated[int, Form()],
    session: Annotated[Session, Depends(get_session)],
    perm: Annotated[PermissionBundle, Depends(_require_user_permission)],
    scope: Annotated[str | None, Form()] = None,
    title: Annotated[str | None, Form()] = None,
) -> UploadAccepted:
    """上传文档（异步入队）。

    同步部分（必须 < 500ms，T18 验收）：
      ① 校验类型 / 大小 / md5 去重（T12，纯 CPU + 一次只读查询）；
      ② 原文写入 MinIO 暂存 key（一次 PUT）；
      ③ 建 parse_tasks 行（insert_task 自带 commit，保证 worker 立即可读）；
      ④ LPUSH 入队。
    之后的解析 / 分块 / 向量化 / 落库全部在 worker 进程里做（T13 编排原样复用）。

    ★ T22c（契约字段接线，行为向后兼容）：
      - `title`（可选）覆盖落库标题，空白串视同未传、仍由文件名推得（见 ingest 编排）；
      - 上传者 `perm.user_id` 随 payload 带进 worker，落 `documents.uploaded_by`。
      ★ title 只改 `documents.title`（展示用）；对象存储 key 与 source_type 仍由
        原文文件名（`validated.ext`）决定，绝不拿自定义标题去推扩展名。

    Raises（全部结构化错误信封）：
        401 UNAUTHENTICATED / 404 NOT_FOUND（kb_id 不存在） /
        409 DUPLICATE_FILE / 413 FILE_TOO_LARGE /
        415 FILE_TYPE_UNSUPPORTED / 503（队列不可用，任务已标 failed）。
    """
    # T22c：上传者身份随任务带进 worker，落 documents.uploaded_by。
    # kb 级写权限（谁能往哪个 kb 传）仍属 V1 的 RBAC 范畴，V0.1 不做。
    uploader_id = perm.user_id

    data = file.file.read()
    # ① 校验（T12）：409/413/415 在此同步抛出，由 main.py 的处理器转错误信封
    validated = validate_upload(
        file.filename or "", data, kb_id=kb_id, session=session
    )

    # scope：显式传入则过白名单（非法 400）；不传则取该知识库的 default_scope
    # （T18b，openapi："不传则用知识库默认值"）。kb 不存在 → 404。
    # ★ 顺序：必须在入队前完成校验，绝不把非法/无效的 scope 写进任务让 worker
    #   踩雷（R7）；"取默认值"与"校验 kb 存在"是同一次查询（见服务层注释）。
    effective_scope = kb_service.resolve_upload_scope(
        session, kb_id=kb_id, scope=scope
    )

    # ② 暂存原文（worker 凭 payload.staging_key 取回）
    task_uuid = str(uuid.uuid4())
    skey = staging_key(task_uuid, validated.title)
    ensure_bucket()
    put_bytes(skey, data)

    # ③ 建任务行（payload 快照 = 重放与排查的全部依据）
    # T22c：title 空白串视同未传（与 openapi 的可选语义一致），落库回落到文件名。
    custom_title = (title or "").strip() or None
    payload = {
        "task_uuid": task_uuid,
        "kb_id": kb_id,
        "filename": validated.title,
        "scope": effective_scope,
        "staging_key": skey,
        "file_md5": validated.file_md5,
        "file_size": validated.file_size,
        "title": custom_title,
        "uploader_id": uploader_id,
    }
    task_id = insert_task(session, payload=payload)

    # ④ 入队。失败必须显式：任务行已建但队列没进 = 静默丢任务（R7 禁止），
    #    因此先把任务标 failed（可查、可重放），再抛错。
    #    错误码用 INTERNAL_ERROR 而非 MODEL_UNAVAILABLE——队列不是模型，
    #    混用会让"模型故障"的监控口径被污染（契约清单没有队列专用码）。
    try:
        enqueue_ingest(task_id)
    except redis.RedisError as exc:
        mark_failed(
            session, task_id, error_reason=f"入队失败（Redis 不可用）：{exc}"
        )
        session.commit()
        raise ApiError(
            500,
            "INTERNAL_ERROR",
            "任务队列暂不可用，请稍后重试",
            {"task_id": task_id},
        ) from exc

    return UploadAccepted(task_id=task_id, doc_id=None, status="pending")
