"""T22 文档列表 / 删除接口的测试。

三层覆盖：
- 服务层（集成，需要 PostgreSQL）：可见性过滤（scope 轴 / kb 轴）、
  kb_id 与 status 过滤、分页、chunk_count 聚合、软删文档不出现、
  status 非法 → 400、scope_keys 为空 → ValueError；
- 删除（集成）：级联三步（真删 chunks / 软删 documents / 删对象前缀）、
  不在可见范围 → 404、不存在 → 404、重复删 → 404；
- 接口层（集成，MinIO / 身份依赖 monkeypatch）：信封形状、401、参数校验。

为什么 MinIO 要打掉：删除会调 `delete_prefix`；本测试验证的是
"顺序与范围"，对象存储的真实往返由 dev 环境端到端验收覆盖
（与 test_kb_crud.py 的 TestKbCascadeDelete 同一原则）。

★ 清理按 `storage_key` 前缀匹配，不能按"文档是否还存在"匹配：
  删除用例会**软删** documents（行保留），按存在性匹配会漏清这些行。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.v1.query import _require_user_permission
from app.main import app
from app.repos.db import get_session
from app.repos.models import Chunk, Document, User
from app.services import document as document_service
from app.services.document import DocumentError
from app.services.permission import PermissionBundle

_TEST_TAG = "t22-test"
_USERNAME = f"{_TEST_TAG}_user"
# 测试用知识库 id：刻意取一个不会与 dev 库真实数据（kb 1/2/3）重叠的值
_KB_ID = 900022
_OTHER_KB_ID = 900023
# ★ 测试专用可见范围：dev 库里存量的 documents 行的 scope 是 `internal`，
#   若测试也用 `internal` 做 scope_keys，会连带命中那些行、断言全部失真。
_SCOPE = f"dept:{_TEST_TAG}部"
_OTHER_SCOPE = f"dept:{_TEST_TAG}他部"


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


def _purge(engine: Engine) -> None:
    """清掉本测试建的全部行，保证可重复跑。

    顺序：chunks → documents → users。
    documents.uploaded_by 有 FK → users.id，users 必须最后删。
    """
    with Session(engine) as session:
        ids = select(Document.id).where(Document.storage_key.like(f"{_TEST_TAG}/%"))
        session.execute(delete(Chunk).where(Chunk.doc_id.in_(ids)))
        session.execute(
            delete(Document).where(Document.storage_key.like(f"{_TEST_TAG}/%"))
        )
        session.execute(delete(User).where(User.username.like(f"{_TEST_TAG}%")))
        session.commit()


@pytest.fixture()
def engine() -> Iterator[Engine]:
    if not _db_available():
        pytest.skip("PostgreSQL 未启动，跳过文档接口集成测试")
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    _purge(eng)
    yield eng
    _purge(eng)


@pytest.fixture()
def user_id(engine: Engine) -> int:
    """建一条真实 users 行：验证列表 LEFT JOIN users 取 username 这条路径。"""
    with Session(engine) as session:
        user = User(
            username=_USERNAME, display_name="T22 测试用户", dept="测试部"
        )
        session.add(user)
        session.commit()
        return user.id


def _seed_doc(
    session: Session,
    *,
    kb_id: int | None = _KB_ID,
    scope: str = _SCOPE,
    status: str = "done",
    chunk_count: int = 2,
    uploaded_by: int | None = None,
    title: str = f"{_TEST_TAG}.md",
    is_active: bool = True,
) -> tuple[int, str]:
    """造一条 documents（可选 chunks），返回 (doc_id, storage_prefix)。"""
    doc_uuid = str(uuid.uuid4())
    prefix = f"{_TEST_TAG}/{doc_uuid}/"
    doc = Document(
        kb_id=kb_id,
        title=title,
        source_type="markdown",
        storage_key=f"{prefix}raw.md",
        parsed_key=f"{prefix}parsed.txt",
        file_md5=uuid.uuid4().hex + uuid.uuid4().hex,  # 32 位，避免与真实行撞
        file_size=10,
        status=status,
        scope=scope,
        uploaded_by=uploaded_by,
        is_active=is_active,
    )
    session.add(doc)
    session.flush()
    for i in range(chunk_count):
        session.add(
            Chunk(
                doc_id=doc.id,
                kb_id=kb_id or 0,
                chunk_index=i,
                content=f"内容 {i}",
                embedding=None,
                scope_key=scope,
            )
        )
    session.commit()
    return doc.id, prefix


class TestDocumentListService:
    def test_lists_visible_active_documents_only(self, engine: Engine) -> None:
        with Session(engine) as session:
            visible, _ = _seed_doc(session, scope=_SCOPE)
            _seed_doc(session, scope=_OTHER_SCOPE)  # 不可见
            _seed_doc(session, scope=_SCOPE, is_active=False)  # 已软删
            rows, total = document_service.list_documents(
                session, scope_keys=[_SCOPE]
            )
            ids = [r.id for r in rows]
            assert ids == [visible]
            assert total == 1

    def test_kb_ids_axis_filters(self, engine: Engine) -> None:
        """kb: 授权下的可见性：kb_ids 非空时只返回该 kb 的文档。"""
        with Session(engine) as session:
            in_kb, _ = _seed_doc(session, kb_id=_KB_ID, scope=_SCOPE)
            _seed_doc(session, kb_id=_OTHER_KB_ID, scope=_SCOPE)
            rows, _ = document_service.list_documents(
                session, scope_keys=[_SCOPE], kb_ids=[_KB_ID]
            )
            assert [r.id for r in rows] == [in_kb]

    def test_kb_id_param_filters(self, engine: Engine) -> None:
        with Session(engine) as session:
            _seed_doc(session, kb_id=_KB_ID)
            other, _ = _seed_doc(session, kb_id=_OTHER_KB_ID)
            rows, total = document_service.list_documents(
                session, scope_keys=[_SCOPE], kb_id=_OTHER_KB_ID
            )
            assert [r.id for r in rows] == [other]
            assert total == 1

    def test_status_filter_and_unknown_status_raises(self, engine: Engine) -> None:
        with Session(engine) as session:
            # 实现里只会出现 done，但过滤逻辑按契约六态工作
            done, _ = _seed_doc(session, status="done")
            _seed_doc(session, status="failed")
            rows, _ = document_service.list_documents(
                session, scope_keys=[_SCOPE], status="done"
            )
            assert [r.id for r in rows] == [done]

            # 空白串视同不传（前端下拉框的空选项）
            rows, _ = document_service.list_documents(
                session, scope_keys=[_SCOPE], status="  "
            )
            assert len(rows) == 2

            with pytest.raises(DocumentError) as ei:
                document_service.list_documents(
                    session, scope_keys=[_SCOPE], status="embedding2"
                )
            assert ei.value.status_code == 400
            assert ei.value.code == "QUESTION_INVALID"

    def test_empty_scope_keys_raises(self, engine: Engine) -> None:
        with Session(engine) as session:
            with pytest.raises(ValueError, match="scope_keys"):
                document_service.list_documents(session, scope_keys=[])

    def test_chunk_count_and_uploaded_by_join(
        self, engine: Engine, user_id: int
    ) -> None:
        with Session(engine) as session:
            doc_id, _ = _seed_doc(session, chunk_count=3, uploaded_by=user_id)
            zero_chunks, _ = _seed_doc(session, chunk_count=0)
            rows, _ = document_service.list_documents(
                session, scope_keys=[_SCOPE]
            )
            by_id = {r.id: r for r in rows}
            assert by_id[doc_id].chunk_count == 3
            # coalesce：没有块的文档返回 0 而不是 None
            assert by_id[zero_chunks].chunk_count == 0
            # uploaded_by 契约里是用户名（LEFT JOIN users）
            assert by_id[doc_id].uploaded_by == _USERNAME

    def test_pagination(self, engine: Engine) -> None:
        with Session(engine) as session:
            for _ in range(3):
                _seed_doc(session)
            page1, total = document_service.list_documents(
                session, scope_keys=[_SCOPE], page=1, page_size=2
            )
            page2, _ = document_service.list_documents(
                session, scope_keys=[_SCOPE], page=2, page_size=2
            )
            assert total == 3
            assert len(page1) == 2
            assert len(page2) == 1
            # 倒序且分页不重叠
            assert {r.id for r in page1}.isdisjoint({r.id for r in page2})


class TestDocumentDelete:
    def test_delete_cascades_three_steps(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[str] = []
        monkeypatch.setattr(
            document_service, "delete_prefix", lambda p: recorded.append(p) or 0
        )
        with Session(engine) as session:
            doc_id, prefix = _seed_doc(session, chunk_count=2)
            deleted = document_service.delete_document(
                session, doc_id=doc_id, scope_keys=[_SCOPE]
            )
            assert deleted == 2
            # ① 对象存储前缀被清理
            assert recorded == [prefix]
            # ② chunks 真删
            assert (
                session.scalar(
                    select(Chunk.id).where(Chunk.doc_id == doc_id)
                )
                is None
            )
            # ③ documents 软删（行保留）
            doc = session.scalar(select(Document).where(Document.id == doc_id))
            assert doc is not None
            assert doc.is_active is False

    def test_delete_out_of_scope_is_404(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不在可见范围内一律 404（不是 403）——403 会泄露"该 id 确实存在"。"""
        monkeypatch.setattr(document_service, "delete_prefix", lambda p: 0)
        with Session(engine) as session:
            doc_id, _ = _seed_doc(session, scope=_OTHER_SCOPE)
            with pytest.raises(DocumentError) as ei:
                document_service.delete_document(
                    session, doc_id=doc_id, scope_keys=[_SCOPE]
                )
            assert ei.value.status_code == 404
            # 未被删除：仍可见（对有权者而言）
            doc = session.scalar(select(Document).where(Document.id == doc_id))
            assert doc is not None and doc.is_active is True

    def test_delete_missing_and_double_delete_are_404(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_service, "delete_prefix", lambda p: 0)
        with Session(engine) as session:
            with pytest.raises(DocumentError) as ei:
                document_service.delete_document(
                    session, doc_id=-1, scope_keys=[_SCOPE]
                )
            assert ei.value.code == "NOT_FOUND"

            doc_id, _ = _seed_doc(session)
            document_service.delete_document(
                session, doc_id=doc_id, scope_keys=[_SCOPE]
            )
            with pytest.raises(DocumentError) as ei2:
                document_service.delete_document(
                    session, doc_id=doc_id, scope_keys=[_SCOPE]
                )
            assert ei2.value.status_code == 404

    def test_delete_empty_scope_keys_raises(self, engine: Engine) -> None:
        with Session(engine) as session:
            with pytest.raises(ValueError, match="scope_keys"):
                document_service.delete_document(
                    session, doc_id=1, scope_keys=[]
                )


class TestDocumentApi:
    @pytest.fixture()
    def client(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[TestClient]:
        eng = engine

        def _override_session() -> Iterator[Session]:
            with Session(eng) as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[_require_user_permission] = lambda: PermissionBundle(
            scope_keys=[_SCOPE], user_id=None
        )
        # 删除会调 delete_prefix 触碰真实对象存储；本测试只验证顺序与范围
        monkeypatch.setattr(document_service, "delete_prefix", lambda p: 0)
        try:
            with TestClient(app) as c:
                yield c
        finally:
            app.dependency_overrides.clear()

    @pytest.fixture()
    def anon_client(self, engine: Engine) -> Iterator[TestClient]:
        """**不覆盖**身份依赖的 client：用于验证缺 X-KB-User 时返回 401。

        ★ 不能复用上面的 client——它覆盖了 `_require_user_permission`
          （返回固定 PermissionBundle），覆盖后 401 分支根本走不到，
          测试会变成"永远通过"的假绿。
        """
        eng = engine

        def _override_session() -> Iterator[Session]:
            with Session(eng) as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        try:
            with TestClient(app) as c:
                yield c
        finally:
            app.dependency_overrides.clear()

    def test_list_returns_envelope(self, client: TestClient, engine: Engine) -> None:
        with Session(engine) as session:
            doc_id, _ = _seed_doc(session, chunk_count=2)
        resp = client.get("/api/v1/documents", headers={"X-KB-User": _USERNAME})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # 契约信封只有 items / total（openapi.yaml L263—274）
        assert set(body) == {"items", "total"}
        item = next(i for i in body["items"] if i["id"] == doc_id)
        assert set(item) == {
            "id",
            "kb_id",
            "title",
            "source_type",
            "file_size",
            "status",
            "scope",
            "chunk_count",
            "error_reason",
            "uploaded_by",
            "created_at",
        }
        assert item["chunk_count"] == 2
        assert item["uploaded_by"] is None  # T22：uploaded_by 暂未打通（另排任务）

    def test_list_requires_identity(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/api/v1/documents")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_list_bad_pagination_and_status(
        self, client: TestClient, engine: Engine
    ) -> None:
        headers = {"X-KB-User": _USERNAME}
        bad_page = client.get("/api/v1/documents?page=0", headers=headers)
        assert bad_page.status_code == 400
        assert (
            client.get("/api/v1/documents?page_size=101", headers=headers).status_code
            == 400
        )
        bad = client.get("/api/v1/documents?status=embedding2", headers=headers)
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "QUESTION_INVALID"

    def test_delete_returns_204_then_404(
        self, client: TestClient, engine: Engine
    ) -> None:
        with Session(engine) as session:
            doc_id, _ = _seed_doc(session, chunk_count=1)
        headers = {"X-KB-User": _USERNAME}
        first = client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
        assert first.status_code == 204, first.text
        second = client.delete(f"/api/v1/documents/{doc_id}", headers=headers)
        assert second.status_code == 404
        assert second.json()["error"]["code"] == "NOT_FOUND"

    def test_delete_out_of_scope_via_api_is_404(
        self, client: TestClient, engine: Engine
    ) -> None:
        with Session(engine) as session:
            doc_id, _ = _seed_doc(session, scope=_OTHER_SCOPE)
        resp = client.delete(
            f"/api/v1/documents/{doc_id}", headers={"X-KB-User": _USERNAME}
        )
        assert resp.status_code == 404
