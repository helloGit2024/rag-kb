"""任务状态路由（V0.1 · T23）。

- GET /api/v1/tasks/{task_id}   查询上传后异步任务（解析＋向量化）的状态

只做：解析路径参数 → 调 task_repo.get_task → 映射响应。
纯读、无业务编排、无状态变更，因此不过 service 层——
与 T18 上传接口直接调 task_repo.insert_task 同一风格。

★ 鉴权口径（T23 决策）：只校身份（401 由依赖挡住），不校任务归属——
  parse_tasks 没有 user 字段，归属模型要动 T18 上传链路
  （OPEN-QUESTIONS #7，已单独排任务）。task_id 可枚举但只暴露
  进度与失败原因，不泄露文档内容；V0.1 内网固定测试账号，风险可接受。
★ 404/401 契约没写（openapi 该端点只定义了 200），按项目惯例实现，
  已备案 OPEN-QUESTIONS 待下次改 openapi 时补响应定义。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.query import ApiError, _require_user_permission
from app.repos.db import get_session
from app.repos.task_repo import TaskNotFoundError, get_task
from app.schemas.tasks import ParseTaskOut
from app.services.permission import PermissionBundle

router = APIRouter(prefix="/api/v1", tags=["tasks"])


@router.get("/tasks/{task_id}", response_model=ParseTaskOut)
def get_task_status(
    task_id: int,
    session: Annotated[Session, Depends(get_session)],
    perm: Annotated[PermissionBundle, Depends(_require_user_permission)],
) -> ParseTaskOut:
    """查询任务状态（前端上传后轮询这个端点看解析进度）。

    error_reason 由 T19 保证是人话（中文、带原因），契约也要求
    "error_reason 必须是给人看的话"。

    Raises:
        ApiError: 404 NOT_FOUND（任务不存在；与文档删除同一惯例——
                  不区分"不存在"与"不可见"）。
    """
    _ = perm
    try:
        task = get_task(session, task_id)
    except TaskNotFoundError as exc:
        raise ApiError(404, "NOT_FOUND", "任务不存在") from exc
    return ParseTaskOut(
        task_id=task.id,
        doc_id=task.doc_id,
        task_type=task.task_type,
        status=task.status,
        progress=task.progress,
        retry_count=task.retry_count,
        error_reason=task.error_reason,
    )
