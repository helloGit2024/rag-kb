"""入库 worker（V0.1 · T18 建，T19 补重试与状态推进）：从 Redis 队列取任务，
跑 T13 的入库编排。

运行方式：`python -m app.workers.ingest_worker`（docker-compose.dev.yml 的
worker 服务就是这个入口）。

主循环一次只做一件事（R10 在线/离线隔离：worker 与在线请求是两个进程）：
    BRPOP 取 task_id → 读 parse_tasks（payload 快照）→ mark_running
    → MinIO 取暂存文件 → ingest_document（T13 原样调用，签名零改动）
    → 成功：mark_done + 回写 doc_id + 删暂存对象
    → 失败：retry_count < 3 → 回退 pending + 重新入队；达到 3 → failed

★ 重试语义（T19，对齐 data-model 2.6「retry_count 最多 3」）：
  retry_count 记"已重试次数"，每次失败 +1；只要仍 < TASK_MAX_RETRY 就重新入队，
  达到上限转终态 failed 并落"人话" error_reason。
  即：最多重试 3 次、总尝试最多 4 次。
  ★ 不做"确定性失败跳过重试"的异常分类（已与项目所有者确认）：坏文件在解析
    阶段就中断、根本不会调嵌入，3 次重试的成本是毫秒级；换来的是"嵌入服务
    抖动"这类瞬时故障能被自动救回——那才是重试真正要解决的问题。

★ 状态与入队的顺序（防僵尸任务）：先 commit(pending)、再 LPUSH。
  反过来一旦入队后进程崩溃，DB 里会留下"pending 但队列里没有"的任务——
  没有任何机制会再碰它，用户永远看到 pending，而且**不报错**（最坏的失败形态）。

★ 暂存对象的清理策略（与方案一致）：
  - 成功后删 `staging/...`（它的使命已完成，正式对象在 docs/ 前缀下）；
  - 重试 / 失败**不删**——留现场给重试与人工排查用。

★ 单任务失败不弄死主循环：异常被兜在"这一条任务"的边界内（标 failed / 重入队
  后继续取下一条）；只有 Redis / DB 这类基础设施故障才会让主循环抛出退出——
  由 compose 的 restart 策略拉起（R7：基础设施错要错得响，不能静默空转）。
  ★ 退出前会把已弹出的 task_id 退回队列（见 main）：task_id 是"先出队、后处理"，
    不退回就成了"DB 里 pending、队列里没有"的僵尸任务——不报错、永远不动。
    重复投递的安全性由 process_one 的终态幂等守卫保证。

★ 并发信号量（T20，R10「离线任务的并发数用信号量限制」）：
  process_one 全程在 _INGEST_SEMAPHORE 保护下执行，许可数读
  INGEST_WORKER_CONCURRENCY（默认 1）。当前主循环是单线程串行，结构上
  并发就是 1——信号量的价值在于：① R10 与 ops-runbook 明确要求这个实体存在；
  ② 将来 worker 改多线程消费时，并发上限已有实体、无需再补。
  ★ 它与嵌入的 pace_s 分工不同：信号量管"worker 同时跑几个任务"，
    pace_s 管"单个任务内嵌入调用给在线链路留多少调度窗口"。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from app.core.queue import enqueue_ingest, pop_ingest
from app.core.storage import delete_object, get_bytes
from app.repos.db import get_engine
from app.repos.models import (
    TASK_MAX_RETRY,
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_TYPE_PARSE,
)
from app.repos.task_repo import (
    get_task,
    mark_done,
    mark_failed,
    mark_pending_for_retry,
    mark_running,
)
from app.services.ingest import ingest_document
from app.services.task_failure import human_reason

# 终态：任务生命周期已结束，重放不应改变它（幂等守卫用，见 process_one）
TERMINAL_TASK_STATUSES = frozenset({TASK_STATUS_DONE, TASK_STATUS_FAILED})

# 崩溃退出前的等待秒数：compose 的 restart 策略没有任何退避，
# 不等一下就会形成"秒级重启 → 立刻再崩"的风暴，把日志刷爆并白烧 CPU
_CRASH_BACKOFF_S = 2.0

# T20 并发信号量（R10）。「单参 getenv + or」写法原因见 embedding.py 注释。
_INGEST_SEMAPHORE = threading.BoundedSemaphore(
    int(os.getenv("INGEST_WORKER_CONCURRENCY") or "1")
)


def _log(event: str, **fields: object) -> None:
    """worker 侧的结构化日志（与 qa.py 的 event=qa 同风格，落 stderr）。"""
    print(
        json.dumps(
            {"event": event, "ts": datetime.now(UTC).isoformat(), **fields},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )


def process_one(session, task_id: int) -> None:
    """消费一条任务。所有"这一份文件的问题"都在此兜住并落成 failed。

    Args:
        session: 调用方提供的 Session（事务边界在本函数内：成功/失败都 commit）。
        task_id: 队列里取出的任务 id。
    """
    # T20：全程持有并发信号量（R10），上限见 _INGEST_SEMAPHORE
    with _INGEST_SEMAPHORE:
        _process_one_guarded(session, task_id)


def _process_one_guarded(session, task_id: int) -> None:
    """process_one 的本体（并发上限由外层信号量保证）。"""
    task = get_task(session, task_id)
    if task.status in TERMINAL_TASK_STATUSES:
        # 幂等守卫：主循环在崩溃时会把同一个 task_id 重新入队（见 main），
        # 若那次其实已经写成终态，重放一次不能重复入库/重复改状态。
        _log(
            "ingest_task",
            task_id=task_id,
            status="skipped",
            reason=f"已是终态 {task.status}",
        )
        return
    if task.task_type != TASK_TYPE_PARSE:
        # R7：队列里出现不认识的任务类型，显式失败而不是猜着跑
        mark_failed(
            session,
            task_id,
            error_reason=f"任务类型无法识别（{task.task_type}），已跳过处理。",
        )
        session.commit()
        return

    payload = task.payload
    mark_running(session, task_id)
    session.commit()

    try:
        data = get_bytes(payload["staging_key"])
        result = ingest_document(
            session,
            kb_id=int(payload["kb_id"]),
            filename=payload["filename"],
            data=data,
            scope=payload.get("scope") or "internal",
        )
    except Exception as exc:
        # 这一份文件的问题（坏文件 / 非 UTF-8 / 嵌入服务挂了……）：
        # SQL 事务先回滚（ingest 内部可能已写过半成品），再由 _handle_failure
        # 决定"重试"还是"终态 failed"。原始错误链照原样进日志（R7，不吞）。
        session.rollback()
        _handle_failure(session, task_id, exc)
        return

    # 成功：任务行 + 删暂存。删暂存失败不该把已成功的任务标 failed——
    # 正式对象已在 docs/ 下，暂存残留只是多占一点空间，记日志即可。
    mark_done(session, task_id, doc_id=result.doc_id)
    session.commit()
    try:
        delete_object(payload["staging_key"])
    except Exception as exc:
        _log(
            "ingest_task",
            task_id=task_id,
            status="done",
            doc_id=result.doc_id,
            staging_cleanup=f"failed: {exc}",
        )
        return
    _log(
        "ingest_task",
        task_id=task_id,
        status="done",
        doc_id=result.doc_id,
        chunk_count=result.chunk_count,
    )


def _handle_failure(session, task_id: int, exc: Exception) -> None:
    """失败处理：未达重试上限则回退 pending 并重新入队，否则落终态 failed。

    ★ 为什么重新 get_task 而不是复用上游那个实例：调用方刚执行过 rollback，
      ORM 实例已过期，直接读 `task.retry_count` 拿到的是陈旧/需重载的值——
      重试计数一旦判断错就是"无限重试"或"永不重试"，两种都不会报错。
      这里统一以"重新查一次"为准。

    ★ 入队失败必须显式转 failed：此时任务行已是 pending 但队列里没有它，
      若不处理就成了"僵尸任务"——用户永远看到 pending 且没有任何报错。
    """
    task = get_task(session, task_id)
    retry_count = task.retry_count
    reason = human_reason(exc)

    if retry_count < TASK_MAX_RETRY:
        new_count = mark_pending_for_retry(session, task_id, error_reason=reason)
        # 先落状态、再入队（顺序原因见模块 docstring）
        session.commit()
        try:
            enqueue_ingest(task_id)
        except Exception as enqueue_exc:
            mark_failed(
                session,
                task_id,
                error_reason=(
                    "重新入队失败（Redis 不可用），任务未能继续重试。"
                    "建议：确认 Redis 恢复后重新上传该文件。"
                ),
            )
            session.commit()
            _log(
                "ingest_task",
                task_id=task_id,
                status="failed",
                retry_count=new_count,
                error="重试入队失败",
                detail=str(enqueue_exc)[:300],
            )
            return
        _log(
            "ingest_task",
            task_id=task_id,
            status="retrying",
            retry_count=new_count,
            error=str(exc)[:300],
        )
        return

    # 重试已耗尽：落终态，error_reason 是人话并注明"已重试 N 次"
    mark_failed(
        session,
        task_id,
        error_reason=human_reason(exc, retried=retry_count),
    )
    session.commit()
    _log(
        "ingest_task",
        task_id=task_id,
        status="failed",
        retry_count=retry_count,
        error=str(exc)[:500],
    )


def main() -> None:
    """主循环：BRPOP 取任务 → process_one。基础设施故障直接抛出（R7）。

    ★ 崩溃时**把已弹出的 task_id 退回队列**（at-most-once → 至少一次）：
      task_id 是在 process_one 之前从 Redis 弹出的，若此间基础设施故障
      （DB 崩了 / Redis 断了），任务行会永久停在 pending 而队列里已经没有它——
      没有任何机制会再碰它，用户只看到"一直在处理中"，且**不报错**
      （2026-10-16 Postgres 崩溃恢复时真实发生过）。
      退回是尽力而为：退不回去也只能记日志退出，但绝不静默丢。
      重复投递的代价由 process_one 的终态幂等守卫兜住。
    """
    SessionLocal = sessionmaker(bind=get_engine())
    _log("ingest_worker_started")
    while True:
        task_id = pop_ingest()
        if task_id is None:
            continue  # BRPOP 超时，空转一圈（能周期性响应退出信号，不忙等）
        try:
            with SessionLocal() as session:
                process_one(session, task_id)
        except Exception as exc:
            # 基础设施故障：先把任务退回队列，再让异常冒出去交给 restart 拉起
            _requeue_after_crash(task_id, exc)
            time.sleep(_CRASH_BACKOFF_S)  # 防 compose 无间隔重启把日志刷爆
            raise


def _requeue_after_crash(task_id: int, exc: Exception) -> None:
    """崩溃退出前把任务退回队列（尽力而为，失败只记日志不掩盖原异常）。"""
    try:
        enqueue_ingest(task_id)
        requeued = True
    except Exception as requeue_exc:
        requeued = False
        _log(
            "ingest_task",
            task_id=task_id,
            status="lost",
            error="崩溃退回队列失败",
            detail=str(requeue_exc)[:300],
        )
    _log(
        "ingest_task",
        task_id=task_id,
        status="requeued" if requeued else "requeue_failed",
        reason=f"worker 崩溃：{type(exc).__name__}",
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("ingest_worker_stopped", reason="KeyboardInterrupt")
    except Exception as exc:
        # 基础设施故障（Redis/DB 连不上等）：错得响，退出交给 compose restart（R7）
        _log("ingest_worker_crashed", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
