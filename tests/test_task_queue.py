"""T18 任务队列 + worker 链路的测试。

三层覆盖：
- 队列（纯单元，FakeRedis 内存实现，不依赖真 Redis）：
  入队/出队往返、非法消息显式失败；
  另有 1 条**真实 Redis** 回归（BRPOP 到点返回 None，防 redis-py 8
  默认 socket_timeout=5s 把 worker 打成崩溃循环，见 TestQueueRealRedis）；
- task_repo（集成，需要 PostgreSQL）：
  insert/get/状态推进（pending→running→done/failed），task_id 自增、
  payload 快照落库、doc_id 回写；
- 上传接口（集成，需要 PostgreSQL；MinIO 与 Redis 都 monkeypatch 掉）：
  202 + task_id、任务行与 payload 落库、415 坏类型、401 无身份。

为什么 MinIO/Redis 要打掉：本测试验证的是"接口 → 任务行 → 入队"这条
**同步**链路（T18 验收"500ms 内返回 task_id"测的就是它），
对象存储与队列的真实往返由 dev 环境端到端验收覆盖（curl 手测），
测试里不引入外部服务依赖（与 test_api_query.py 的 _no_redis 同一原则）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.v1.documents import (
    enqueue_ingest as _real_enqueue,
)
from app.api.v1.query import _require_user_permission
from app.core import queue as queue_mod
from app.main import app
from app.repos.db import get_session
from app.repos.models import (
    TASK_STATUS_DONE,
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    ParseTask,
)
from app.repos.task_repo import (
    TaskNotFoundError,
    get_task,
    insert_task,
    mark_done,
    mark_failed,
    mark_running,
)
from app.services.permission import PermissionBundle


# ── 内存版 Redis（只实现本测试用到的 lpush/brpop）────────────────────
class _FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}

    def lpush(self, key: str, value: str) -> int:
        return self.lists.setdefault(key, []).insert(0, value) or 1

    def brpop(self, key: str, timeout: int = 0):
        items = self.lists.get(key) or []
        if not items:
            return None
        return (key, items.pop())


class TestQueue:
    """队列语义：只传 task_id，非法消息显式失败（R7）。"""

    def test_enqueue_then_pop_roundtrip(self) -> None:
        fake = _FakeRedis()
        queue_mod.enqueue_ingest(42, client=fake)  # type: ignore[arg-type]
        assert queue_mod.pop_ingest(client=fake) == 42  # type: ignore[arg-type]
        # 取空后返回 None（BRPOP 超时语义）
        assert queue_mod.pop_ingest(client=fake) is None  # type: ignore[arg-type]

    def test_fifo_order(self) -> None:
        """LPUSH + BRPOP = FIFO：先进队的 task_id 先被 worker 拿到。"""
        fake = _FakeRedis()
        for tid in (1, 2, 3):
            queue_mod.enqueue_ingest(tid, client=fake)  # type: ignore[arg-type]
        got = [queue_mod.pop_ingest(client=fake) for _ in range(3)]  # type: ignore[arg-type]
        assert got == [1, 2, 3]

    def test_invalid_message_raises(self) -> None:
        fake = _FakeRedis()
        fake.lpush(queue_mod.QUEUE_KEY, "not-a-task-id")
        with pytest.raises(ValueError, match="非法 task_id"):
            queue_mod.pop_ingest(client=fake)  # type: ignore[arg-type]


def _redis_available() -> bool:
    try:
        queue_mod._queue_client().ping()
    except Exception:
        return False
    return True


class TestQueueRealRedis:
    """回归（集成，需要真 Redis）：BRPOP 到点必须返回 None，不得抛 TimeoutError。

    背景：redis-py 8 起 `from_url` 的 socket_timeout **默认 5s**（8 之前为 None），
    BRPOP 阻塞 5s 时客户端与服务端同时到点、客户端先超时——worker 每 5s
    崩溃重启一次（e2e 实测，详见 app/core/queue.py 模块 docstring）。
    FakeRedis 测不出这个（不过 socket），必须打真 Redis。
    """

    def test_pop_timeout_returns_none_not_raise(self) -> None:
        if not _redis_available():
            pytest.skip("Redis 未启动，跳过真实出队回归测试")
        # timeout_s=1：验证"到点返回 None"的语义，1s 足够触发且不让测试变慢。
        # 队列在生产/测试中都只有 worker 与上传接口触碰，此处跑在空队列上。
        assert queue_mod.pop_ingest(timeout_s=1) is None


# ── 集成：task_repo 状态推进 ─────────────────────────────────────────
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


_TEST_TAG = "t18-test"


def _purge(engine: Engine) -> None:
    """清掉本测试建的任务行，保证可重复跑。

    ★ 必须按 payload 里的 **filename** 前缀匹配，不能按 task_uuid：
    TestUploadApi 走真实上传端点，端点用 uuid4 生成 task_uuid（不以 t18-test
    开头），按 uuid 清理会让这些行永久残留在 dev 库（e2e 时实测漏了 2 行）。
    两类测试的 filename 都带 `t18-test` 前缀（_payload 用 t18-test.md，
    上传接口测试用 t18-test-api.md），统一覆盖。
    """
    with Session(engine) as session:
        session.execute(
            delete(ParseTask).where(
                ParseTask.payload["filename"].astext.like(f"{_TEST_TAG}%")
            )
        )
        session.commit()


def _payload(suffix: str = "") -> dict:
    return {
        "task_uuid": f"{_TEST_TAG}-{suffix}",
        "kb_id": 1,
        "filename": f"{_TEST_TAG}.md",
        "scope": "internal",
        "staging_key": f"staging/{_TEST_TAG}/{_TEST_TAG}.md",
        "file_md5": "0" * 32,
        "file_size": 10,
    }


@pytest.fixture()
def engine() -> Iterator[Engine]:
    if not _db_available():
        pytest.skip("PostgreSQL 未启动，跳过任务队列集成测试")
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    _purge(eng)
    yield eng
    _purge(eng)


class TestTaskRepo:
    def test_insert_and_get(self, engine: Engine) -> None:
        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("a"))
            task = get_task(session, task_id)
            assert task.status == "pending"
            assert task.retry_count == 0
            assert task.payload["kb_id"] == 1

    def test_get_missing_raises(self, engine: Engine) -> None:
        with Session(engine) as session:
            with pytest.raises(TaskNotFoundError):
                get_task(session, -1)

    def test_full_lifecycle_done(self, engine: Engine) -> None:
        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("b"))
            mark_running(session, task_id)
            session.commit()
            task = get_task(session, task_id)
            assert task.status == TASK_STATUS_RUNNING
            assert task.started_at is not None

            mark_done(session, task_id, doc_id=999)
            session.commit()
            task = get_task(session, task_id)
            assert task.status == TASK_STATUS_DONE
            assert task.doc_id == 999
            assert task.progress == 100
            assert task.finished_at is not None

    def test_lifecycle_failed(self, engine: Engine) -> None:
        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("c"))
            mark_running(session, task_id)
            mark_failed(
                session, task_id, error_reason="IngestError: 文件不是 UTF-8 编码"
            )
            session.commit()
            task = get_task(session, task_id)
            assert task.status == TASK_STATUS_FAILED
            assert "UTF-8" in (task.error_reason or "")
            assert task.finished_at is not None


# ── 集成：上传接口（MinIO / Redis monkeypatch 掉）────────────────────
class TestUploadApi:
    @pytest.fixture()
    def client(self, engine: Engine) -> Iterator[TestClient]:
        eng = engine
        enqueued: list[int] = []

        def _override_session() -> Iterator[Session]:
            with Session(eng) as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[_require_user_permission] = lambda: PermissionBundle(
            scope_keys=["internal"]
        )
        # MinIO / Redis 不打真实服务（见模块 docstring）
        import app.api.v1.documents as documents_mod

        documents_mod.ensure_bucket = lambda **_: "ragkb"  # type: ignore[attr-defined]
        documents_mod.put_bytes = lambda *a, **k: a[0]  # type: ignore[attr-defined]
        documents_mod.enqueue_ingest = lambda task_id: enqueued.append(task_id)  # type: ignore[attr-defined]
        try:
            with TestClient(app) as c:
                c.enqueued_task_ids = enqueued  # type: ignore[attr-defined]
                yield c
        finally:
            app.dependency_overrides.clear()
            documents_mod.ensure_bucket = __import__(
                "app.core.storage", fromlist=["ensure_bucket"]
            ).ensure_bucket  # type: ignore[attr-defined]
            documents_mod.put_bytes = __import__(
                "app.core.storage", fromlist=["put_bytes"]
            ).put_bytes  # type: ignore[attr-defined]
            documents_mod.enqueue_ingest = _real_enqueue  # type: ignore[attr-defined]

    def test_upload_returns_202_with_task_id(
        self, client: TestClient, engine: Engine
    ) -> None:
        md_content = b"# hello\n\nworld"
        resp = client.post(
            "/api/v1/documents",
            files={"file": (f"{_TEST_TAG}-api.md", md_content, "text/markdown")},
            data={"kb_id": "1"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        task_id = body["task_id"]
        assert isinstance(task_id, int) and task_id > 0
        assert body["status"] == "pending"
        assert body["doc_id"] is None

        # 任务行落库 + payload 快照完整
        with Session(engine) as session:
            task = get_task(session, task_id)
            assert task.status == "pending"
            assert task.payload["kb_id"] == 1
            assert task.payload["filename"].endswith(".md")
            assert task.payload["staging_key"].startswith("staging/")

        # 且已入队（fake 记录到了同一个 task_id）
        assert client.enqueued_task_ids == [task_id]  # type: ignore[attr-defined]

    def test_upload_rejects_bad_type(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/documents",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
            data={"kb_id": "1"},
        )
        assert resp.status_code == 415
        assert resp.json()["error"]["code"] == "FILE_TYPE_UNSUPPORTED"

    def test_upload_rejects_invalid_scope(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/documents",
            files={"file": (f"{_TEST_TAG}-scope.md", b"x", "text/markdown")},
            data={"kb_id": "1", "scope": "dept:"},
        )
        assert resp.status_code == 400

    def test_upload_requires_auth(self, engine: Engine) -> None:
        # 不覆盖 _require_user_permission，也不带 X-KB-User → 401
        app.dependency_overrides.clear()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/api/v1/documents",
                    files={"file": ("a.md", b"x", "text/markdown")},
                    data={"kb_id": "1"},
                )
            assert resp.status_code == 401
            assert resp.json()["error"]["code"] == "UNAUTHENTICATED"
        finally:
            app.dependency_overrides.clear()


# ── 集成：任务状态查询接口（T23；纯读库，MinIO/Redis 均不触碰）────────
class TestTaskStatusApi:
    """GET /api/v1/tasks/{task_id}：200 各状态 / 404 / 401。

    鉴权口径（T23 决策）：只校身份不校归属——parse_tasks 无 user 字段，
    归属模型待 OPEN-QUESTIONS #7 打通 uploaded_by 后一并补。
    """

    @pytest.fixture()
    def client(self, engine: Engine) -> Iterator[TestClient]:
        def _override_session() -> Iterator[Session]:
            with Session(engine) as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[_require_user_permission] = lambda: PermissionBundle(
            scope_keys=["internal"]
        )
        try:
            with TestClient(app) as c:
                yield c
        finally:
            app.dependency_overrides.clear()

    def test_pending_task_shape(self, client: TestClient, engine: Engine) -> None:
        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("status-a"))
        resp = client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 字段名逐一对齐 openapi components.schemas.ParseTask
        assert body["task_id"] == task_id
        assert body["doc_id"] is None  # worker 未跑完，还没有文档
        assert body["task_type"] == "parse"
        assert body["status"] == "pending"
        assert body["progress"] == 0
        assert body["retry_count"] == 0
        assert body["error_reason"] is None
        # payload（staging_key 等内部信息）不得出现在响应里
        assert "payload" not in body

    def test_done_task_shape(self, client: TestClient, engine: Engine) -> None:
        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("status-b"))
            mark_done(session, task_id, doc_id=42)
            session.commit()
        resp = client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == TASK_STATUS_DONE
        assert body["doc_id"] == 42
        assert body["progress"] == 100

    def test_failed_task_reason(self, client: TestClient, engine: Engine) -> None:
        with Session(engine) as session:
            task_id = insert_task(session, payload=_payload("status-c"))
            mark_failed(session, task_id, error_reason="文件不是 UTF-8 编码")
            session.commit()
        resp = client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == TASK_STATUS_FAILED
        # 契约："error_reason 必须是给人看的话"（T19 中文原因落库）
        assert body["error_reason"] == "文件不是 UTF-8 编码"

    def test_missing_task_returns_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/tasks/999999999")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_requires_auth(self, engine: Engine) -> None:
        # 不覆盖 _require_user_permission，也不带 X-KB-User → 401
        app.dependency_overrides.clear()
        try:
            with TestClient(app) as c:
                resp = c.get("/api/v1/tasks/1")
            assert resp.status_code == 401
            assert resp.json()["error"]["code"] == "UNAUTHENTICATED"
        finally:
            app.dependency_overrides.clear()
