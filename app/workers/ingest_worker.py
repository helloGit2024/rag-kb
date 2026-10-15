"""入库 worker（V0.1 · T18）：从 Redis 队列取任务，跑 T13 的入库编排。

运行方式：`python -m app.workers.ingest_worker`（docker-compose.dev.yml 的
worker 服务就是这个入口）。

主循环一次只做一件事（R10 在线/离线隔离：worker 与在线请求是两个进程）：
    BRPOP 取 task_id → 读 parse_tasks（payload 快照）→ mark_running
    → MinIO 取暂存文件 → ingest_document（T13 原样调用，签名零改动）
    → 成功：mark_done + 回写 doc_id + 删暂存对象
    → 失败：mark_failed（如实记录原始错误；重试 3 次属 T19，本版单次执行）

★ 暂存对象的清理策略（与方案一致）：
  - 成功后删 `staging/...`（它的使命已完成，正式对象在 docs/ 前缀下）；
  - 失败**不删**——留现场给 T19 的重试与人工排查用。

★ 单任务失败不弄死主循环：异常被兜在"这一条任务"的边界内（标 failed 后继续
  取下一条）；只有 Redis / DB 这类基础设施故障才会让主循环抛出退出——
  由 compose 的 restart 策略拉起（R7：基础设施错要错得响，不能静默空转）。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from app.core.queue import pop_ingest
from app.core.storage import delete_object, get_bytes
from app.repos.db import get_engine
from app.repos.models import TASK_TYPE_PARSE
from app.repos.task_repo import (
    get_task,
    mark_done,
    mark_failed,
    mark_running,
)
from app.services.ingest import ingest_document


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
    task = get_task(session, task_id)
    if task.task_type != TASK_TYPE_PARSE:
        # R7：队列里出现不认识的任务类型，显式失败而不是猜着跑
        mark_failed(session, task_id, error_reason=f"未知任务类型：{task.task_type!r}")
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
        # 如实记录原始错误链，不美化不吞掉（R7）。重试与人话化属 T19。
        session.rollback()
        mark_failed(session, task_id, error_reason=f"{type(exc).__name__}: {exc}")
        session.commit()
        _log(
            "ingest_task",
            task_id=task_id,
            status="failed",
            error=str(exc)[:500],
        )
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


def main() -> None:
    """主循环：BRPOP 取任务 → process_one。基础设施故障直接抛出（R7）。"""
    SessionLocal = sessionmaker(bind=get_engine())
    _log("ingest_worker_started")
    while True:
        task_id = pop_ingest()
        if task_id is None:
            continue  # BRPOP 超时，空转一圈（能周期性响应退出信号，不忙等）
        with SessionLocal() as session:
            process_one(session, task_id)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("ingest_worker_stopped", reason="KeyboardInterrupt")
    except Exception as exc:
        # 基础设施故障（Redis/DB 连不上等）：错得响，退出交给 compose restart（R7）
        _log("ingest_worker_crashed", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
