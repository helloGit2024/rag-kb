"""parse_tasks 表数据访问（V0.1 · T18）。

★ app/repos/ 是唯一允许出现 SQL 的层（目录约定）。
本模块提供任务的创建 / 读取 / 状态推进；状态机的**决策**（何时算失败、
要不要重试）在 worker 编排层，不在这一层。

★ 除 insert_task 外都不 commit：事务边界由调用方控制。
  insert_task 例外地自带 commit——上传接口要求"建完任务行立即可见"，
  因为紧接着就要把 task_id 入队，worker 可能立刻来读。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.repos.models import (
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_RUNNING,
    TASK_TYPE_PARSE,
    ParseTask,
)


class TaskNotFoundError(LookupError):
    """任务 id 不存在（R7：显式失败，不返回 None 让调用方猜）。"""


def insert_task(session: Session, *, payload: dict) -> int:
    """创建一条 pending 任务并 **立即 commit**，返回 task_id。

    ★ 为什么这里自己 commit（与 insert_document 的"不 commit"约定相反）：
      上传接口的下一步是把 task_id LPUSH 进 Redis，worker 随时可能 BRPOP 拿走
      并来读这一行。不先 commit，worker 会读到"任务不存在"。
      任务行与 documents 行没有共生关系（doc 行由 worker 侧 ingest 创建），
      所以这里独立提交是安全的。
    """
    task = ParseTask(task_type=TASK_TYPE_PARSE, payload=payload)
    session.add(task)
    session.commit()
    return task.id


def get_task(session: Session, task_id: int) -> ParseTask:
    """按 id 取任务；不存在抛 TaskNotFoundError（R7 显式失败）。"""
    task = session.execute(
        select(ParseTask).where(ParseTask.id == task_id)
    ).scalar_one_or_none()
    if task is None:
        raise TaskNotFoundError(f"任务不存在：task_id={task_id}")
    return task


def mark_running(session: Session, task_id: int) -> None:
    """pending → running，记录 started_at。不 commit（worker 统一控制事务）。"""
    task = get_task(session, task_id)
    task.status = TASK_STATUS_RUNNING
    task.started_at = datetime.now(UTC)
    session.flush()


def mark_done(session: Session, task_id: int, *, doc_id: int) -> None:
    """running → done，回写 doc_id 与 finished_at，progress 置 100。"""
    task = get_task(session, task_id)
    task.status = TASK_STATUS_DONE
    task.doc_id = doc_id
    task.progress = 100
    task.finished_at = datetime.now(UTC)
    session.flush()


def mark_failed(session: Session, task_id: int, *, error_reason: str) -> None:
    """running → failed（终态），落 error_reason。

    ★ error_reason 必须是"人话"（api-contract 5.4 / AC-02）。
      worker 侧统一经 app/services/task_failure.human_reason 转换后传入，
      本层只负责落库，不做文案（分层：SQL 层不管文案，业务层不管 SQL）。
    """
    task = get_task(session, task_id)
    task.status = TASK_STATUS_FAILED
    task.error_reason = error_reason
    task.finished_at = datetime.now(UTC)
    session.flush()


def mark_pending_for_retry(
    session: Session, task_id: int, *, error_reason: str
) -> int:
    """失败后回退成 pending 以便重试：retry_count += 1，返回新值。

    ★ 语义（data-model 2.6「retry_count 最多 3」）：retry_count 记"已重试次数"，
      worker 判 `retry_count < TASK_MAX_RETRY` 才重试，达到上限转 mark_failed。
    ★ progress 归 0：整体完成度回到起点（本版不做阶段进度，属 T19 方案口径）。
    ★ finished_at 置空：任务并未终结，终结时间只属于 done / failed。
    ★ 不 commit：worker 需要控制"先落状态、再入队"的顺序（见 ingest_worker）。
    """
    task = get_task(session, task_id)
    task.status = TASK_STATUS_PENDING
    task.retry_count += 1
    task.error_reason = error_reason
    task.progress = 0
    task.finished_at = None
    session.flush()
    return task.retry_count
