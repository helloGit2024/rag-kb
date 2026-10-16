"""T19 任务重试与失败原因（"人话"）的测试。

三层覆盖：
- 人话映射（纯单元，不依赖 DB）：各类异常 → 中文句子，且不带异常类名前缀；
- task_repo 重试回退（集成，需要 PostgreSQL）：retry_count 递增、状态回 pending、
  progress 归 0、finished_at 清空；
- worker 重试闭环（集成，需要 PostgreSQL；MinIO / Redis / ingest 均 monkeypatch）：
  失败 → 回退 pending 并重新入队，累计 3 次后转终态 failed 且 error_reason 是人话；
  入队失败 → 立即转 failed（不留"pending 但队列里没有"的僵尸任务）；
  成功路径仍写 done（确认 T18 行为没被改坏）。

★ 为什么这样构造"坏文件"：用一个真实非 UTF-8 字节走 **真实 ingest_document**
  （只 monkeypatch 取文件这一步），验证的是"用户上传坏文件 → 系统给出人话"的
  完整链路；该路径在解析阶段就中断，不调嵌入服务，因此测试不依赖 Ollama。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import redis
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.embedding import EmbeddingError
from app.repos.models import (
    TASK_MAX_RETRY,
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    ParseTask,
)
from app.repos.task_repo import (
    get_task,
    insert_task,
    mark_failed,
    mark_pending_for_retry,
)
from app.services.file_validator import FileValidationError
from app.services.ingest import IngestError, IngestResult
from app.services.permission import ScopeError
from app.services.task_failure import human_reason
from app.workers import ingest_worker


# ── 纯单元：异常 → 人话 ──────────────────────────────────────────────
class TestHumanReason:
    """验收"error_reason 是人话"的第一道防线：文案层本身。"""

    @pytest.mark.parametrize(
        ("exc", "expected_fragments"),
        [
            (
                FileValidationError(
                    415,
                    "FILE_TYPE_UNSUPPORTED",
                    "不支持的文件类型：.exe（V0.1 仅支持 .md / .txt）",
                ),
                ["不支持的文件类型", ".exe"],
            ),
            (ScopeError("非法 scope：dept:"), ["可见范围", "建议"]),
            (
                UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
                ["UTF-8", "建议"],
            ),
            (IngestError("解析后内容为空，拒绝入库：a.md"), ["内容为空", "建议"]),
            (EmbeddingError("嵌入服务不可达（http://x:11434）"), ["嵌入服务", "建议"]),
            (ValueError("boom"), ["处理失败", "boom", "建议"]),
        ],
    )
    def test_maps_to_human_text(
        self, exc: Exception, expected_fragments: list[str]
    ) -> None:
        reason = human_reason(exc)
        for fragment in expected_fragments:
            assert fragment in reason, reason
        # 核心验收点：不能是 "XxxError: ..." 这种给机器看的字串
        assert not reason.startswith(type(exc).__name__), reason

    def test_retried_suffix_mentions_attempts(self) -> None:
        reason = human_reason(EmbeddingError("嵌入服务不可达"), retried=3)
        assert "已自动重试 3 次" in reason
        # 人话主体仍在，不能被后缀挤掉
        assert "嵌入服务" in reason

    def test_no_retry_suffix_when_zero(self) -> None:
        assert "重试" not in human_reason(EmbeddingError("嵌入服务不可达"))


# ── 集成：task_repo 重试回退 ─────────────────────────────────────────
def _db_available() -> bool:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return False
    try:
        eng = create_engine(url, pool_pre_ping=True)
        with eng.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except Exception:
        return False
    return True


_TEST_TAG = "t19-test"


def _purge(engine: Engine) -> None:
    """清掉本测试建的任务行（按 payload.filename 前缀，与 test_task_queue 同款）。"""
    with Session(engine) as session:
        session.execute(
            delete(ParseTask).where(
                ParseTask.payload["filename"].astext.like(f"{_TEST_TAG}%")
            )
        )
        session.commit()


def _payload(suffix: str, *, filename: str | None = None) -> dict:
    return {
        "task_uuid": f"{_TEST_TAG}-{suffix}",
        "kb_id": 1,
        "filename": filename or f"{_TEST_TAG}-{suffix}.md",
        "scope": "internal",
        "staging_key": f"staging/{_TEST_TAG}/{suffix}",
        "file_md5": "0" * 32,
        "file_size": 10,
    }


@pytest.fixture()
def engine() -> Iterator[Engine]:
    if not _db_available():
        pytest.skip("PostgreSQL 未启动，跳过 T19 重试集成测试")
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    _purge(eng)
    yield eng
    _purge(eng)


class TestMarkPendingForRetry:
    def test_increments_and_resets(self, engine: Engine) -> None:
        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("repo"))
            # 先把状态推到 running / 有进度，验证回退时被清干净
            task = get_task(session, task_id)
            task.status = "running"
            task.progress = 42
            session.flush()

            new_count = mark_pending_for_retry(
                session, task_id, error_reason="嵌入服务不可用"
            )
            session.commit()
            assert new_count == 1

            task = get_task(session, task_id)
            assert task.status == TASK_STATUS_PENDING
            assert task.retry_count == 1
            assert task.progress == 0
            assert task.finished_at is None
            assert task.error_reason == "嵌入服务不可用"

            # 再次回退 → 计数继续累加
            assert (
                mark_pending_for_retry(session, task_id, error_reason="x") == 2
            )


# ── 集成：worker 重试闭环 ─────────────────────────────────────────────
class TestWorkerRetry:
    def _patch_failing_ingest(
        self, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> list[int]:
        """让 get_bytes 成功、ingest 固定抛 exc、入队只记录不落 Redis。"""
        enqueued: list[int] = []

        def _boom(*args: object, **kwargs: object) -> None:
            raise exc

        monkeypatch.setattr(ingest_worker, "get_bytes", lambda key: b"x")
        monkeypatch.setattr(ingest_worker, "ingest_document", _boom)
        monkeypatch.setattr(
            ingest_worker, "enqueue_ingest", lambda task_id: enqueued.append(task_id)
        )
        return enqueued

    def test_retries_three_times_then_fails(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        enqueued = self._patch_failing_ingest(
            monkeypatch, EmbeddingError("嵌入服务不可达（http://localhost:11434）")
        )
        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("retry"))

        # 前 3 次：每次回退 pending + 重新入队，retry_count 逐次 +1
        for attempt in range(1, TASK_MAX_RETRY + 1):
            with Session(engine) as session:
                ingest_worker.process_one(session, task_id)
            with Session(engine) as session:
                task = get_task(session, task_id)
                assert task.status == TASK_STATUS_PENDING, f"第 {attempt} 次尝试"
                assert task.retry_count == attempt
                assert task.finished_at is None
            assert enqueued == [task_id] * attempt

        # 第 4 次（重试耗尽）：终态 failed + 人话 + 不再入队
        with Session(engine) as session:
            ingest_worker.process_one(session, task_id)
        with Session(engine) as session:
            task = get_task(session, task_id)
            assert task.status == TASK_STATUS_FAILED
            assert task.retry_count == TASK_MAX_RETRY
            assert task.finished_at is not None
            reason = task.error_reason or ""
            assert "嵌入服务" in reason
            assert "已自动重试 3 次" in reason
            # 不能带异常类名前缀（验收：人话）
            assert not reason.startswith("EmbeddingError")
        assert enqueued == [task_id] * TASK_MAX_RETRY

    def test_bad_file_ends_failed_with_human_reason(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """真实 ingest_document + 真实非 UTF-8 字节 → 最终 failed 且是人话。

        ★ 只 monkeypatch `get_bytes`（取暂存文件那一步），解析/校验走真实代码，
          否则这条测试就变成"我自己抛异常自己断言"，测不到真链路。
        """
        enqueued: list[int] = []
        monkeypatch.setattr(
            ingest_worker,
            "get_bytes",
            lambda key: b"\xff\xfe\x00\x01 not-utf8-at-all",
        )
        monkeypatch.setattr(
            ingest_worker, "enqueue_ingest", lambda task_id: enqueued.append(task_id)
        )

        with Session(engine) as session:
            task_id = insert_task(
                session, payload=_payload("bad", filename=f"{_TEST_TAG}-bad.txt")
            )

        for _ in range(TASK_MAX_RETRY + 1):
            with Session(engine) as session:
                ingest_worker.process_one(session, task_id)

        with Session(engine) as session:
            task = get_task(session, task_id)
            assert task.status == TASK_STATUS_FAILED
            assert task.retry_count == TASK_MAX_RETRY
            reason = task.error_reason or ""
            assert "UTF-8" in reason
            assert "建议" in reason
            assert not reason.startswith("IngestError")

    def test_enqueue_failure_marks_failed_not_zombie(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重试入队失败 → 立即 failed：不能留下"pending 但队列里没有"的僵尸任务。"""
        monkeypatch.setattr(ingest_worker, "get_bytes", lambda key: b"x")

        def _boom(*args: object, **kwargs: object) -> None:
            raise ValueError("boom")

        def _enqueue_down(task_id: int) -> None:
            raise redis.RedisError("Redis 连接失败")

        monkeypatch.setattr(ingest_worker, "ingest_document", _boom)
        monkeypatch.setattr(ingest_worker, "enqueue_ingest", _enqueue_down)

        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("enqdown"))

        with Session(engine) as session:
            ingest_worker.process_one(session, task_id)

        with Session(engine) as session:
            task = get_task(session, task_id)
            assert task.status == TASK_STATUS_FAILED
            assert task.retry_count == 1
            assert "Redis" in (task.error_reason or "")

    def test_success_still_marks_done(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T18 的成功路径不能被 T19 改坏：done + doc_id + 进度 100 + 清暂存。"""
        deleted: list[str] = []
        payload = _payload("ok")
        monkeypatch.setattr(ingest_worker, "get_bytes", lambda key: b"# hi")

        def _fake_ingest(session: Session, **kwargs: object) -> IngestResult:
            return IngestResult(
                doc_id=4242,
                doc_uuid="uuid",
                chunk_count=1,
                vector_count=1,
                raw_key="docs/uuid/raw.md",
                parsed_key="docs/uuid/parsed.txt",
            )

        monkeypatch.setattr(ingest_worker, "ingest_document", _fake_ingest)
        monkeypatch.setattr(
            ingest_worker, "delete_object", lambda key: deleted.append(key)
        )

        with Session(engine) as session:
            task_id = insert_task(session, payload=payload)
        with Session(engine) as session:
            ingest_worker.process_one(session, task_id)

        with Session(engine) as session:
            task = get_task(session, task_id)
            assert task.status == TASK_STATUS_DONE
            assert task.doc_id == 4242
            assert task.progress == 100
            assert task.retry_count == 0
        assert deleted == [payload["staging_key"]]

    def test_unknown_task_type_fails(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ingest_worker, "get_bytes", lambda key: b"x")
        with Session(engine) as session:
            task = ParseTask(
                task_type="eval", payload=_payload("unknown")
            )
            session.add(task)
            session.commit()
            task_id = task.id

        with Session(engine) as session:
            ingest_worker.process_one(session, task_id)

        with Session(engine) as session:
            task = get_task(session, task_id)
            assert task.status == TASK_STATUS_FAILED
            assert "无法识别" in (task.error_reason or "")
            assert task.retry_count == 0

    def test_terminal_task_is_skipped(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """幂等守卫：已是终态的任务被重放时不能再跑一遍（重复投递的兜底）。

        崩溃退回队列（main → _requeue_after_crash）会产生重复投递，
        若那次其实已经写成 failed/done，重放必须无副作用。
        """
        called: list[int] = []

        def _spy(*args: object, **kwargs: object) -> None:
            called.append(1)
            raise AssertionError("终态任务不应再执行入库")

        monkeypatch.setattr(ingest_worker, "get_bytes", lambda key: b"x")
        monkeypatch.setattr(ingest_worker, "ingest_document", _spy)

        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("terminal"))
            mark_failed(session, task_id, error_reason="先前已失败")
            session.commit()

        with Session(engine) as session:
            ingest_worker.process_one(session, task_id)

        assert called == []
        with Session(engine) as session:
            task = get_task(session, task_id)
            # 状态与原因都没被重放改写
            assert task.status == TASK_STATUS_FAILED
            assert task.error_reason == "先前已失败"
            assert task.retry_count == 0


# ── 崩溃不丢任务（main 的退回队列）────────────────────────────────────
class TestCrashRequeue:
    def test_main_requeues_popped_task_on_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """基础设施故障把已弹出的 task_id 退回队列，再让异常冒出去重启。"""
        enqueued: list[int] = []

        def _boom(session: object, task_id: int) -> None:
            raise RuntimeError("DB 崩了")

        monkeypatch.setattr(ingest_worker, "pop_ingest", lambda: 7)
        monkeypatch.setattr(ingest_worker, "process_one", _boom)
        monkeypatch.setattr(
            ingest_worker, "enqueue_ingest", lambda task_id: enqueued.append(task_id)
        )
        monkeypatch.setattr(ingest_worker, "_CRASH_BACKOFF_S", 0)

        # 异常必须继续冒出去（交给 compose restart），但任务已先退回队列
        with pytest.raises(RuntimeError, match="DB 崩了"):
            ingest_worker.main()

        assert enqueued == [7]

    def test_requeue_failure_does_not_swallow_original_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """退回失败也不能抛：否则会把真正的崩溃原因掩盖掉（R7 可定位）。"""

        def _down(task_id: int) -> None:
            raise redis.RedisError("Redis 也挂了")

        monkeypatch.setattr(ingest_worker, "enqueue_ingest", _down)
        # 不抛异常即通过
        ingest_worker._requeue_after_crash(7, RuntimeError("DB 崩了"))
