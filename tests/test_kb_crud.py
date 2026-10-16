"""T21 知识库 CRUD 接口与级联删除的测试。

三层覆盖（全部不依赖外部服务；对象存储按需 monkeypatch）：
- service（集成，需要 PostgreSQL）：创建/改名判重（409）、scope 白名单（400）、
  空白名（400）、404、分页、以及**级联删除三步**（清对象 → 删 chunks →
  软删 documents → 删库行）；
- api（集成，TestClient）：4 个端点的状态码与响应字段形状、401、
  重名 409、非法 scope 400、分页参数越界 400、created_by 落库；
- 判重走的是**数据库 UNIQUE 约束**这条路径：测试必须真的插两次同名，
  而不是只测"预检查返回 True"。

清理准则：所有测试数据带 `t21-test` 标记（知识库名 / 文档 storage_key），
按标记清理，保证可重复跑——★ 级联删除会把 documents 软删（is_active=false）
而不是删行，那些行必须也能被清理掉，所以按 **storage_key 前缀**匹配，
不能按"知识库是否还存在"匹配（库已删、行还在）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.v1.query import _require_user_permission
from app.main import app
from app.repos.db import get_session
from app.repos.models import Chunk, Document, KnowledgeBase, User
from app.repos.user_repo import find_user_by_username
from app.services import kb as kb_service
from app.services.kb import KbError
from app.services.permission import PermissionBundle

_TEST_TAG = "t21-test"
_USERNAME = "t21_test_user"


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
    with Session(engine) as session:
        # 先按 storage_key 标记清文档与向量：级联删除留下的软删文档也要清掉
        doc_ids = select(Document.id).where(
            Document.storage_key.like(f"{_TEST_TAG}/%")
        )
        session.execute(delete(Chunk).where(Chunk.doc_id.in_(doc_ids)))
        session.execute(
            delete(Document).where(Document.storage_key.like(f"{_TEST_TAG}/%"))
        )
        session.execute(
            delete(KnowledgeBase).where(KnowledgeBase.name.like(f"{_TEST_TAG}%"))
        )
        user = find_user_by_username(session, username=_USERNAME)
        if user is not None:
            session.execute(delete(User).where(User.id == user.id))
        session.commit()


@pytest.fixture()
def engine() -> Iterator[Engine]:
    if not _db_available():
        pytest.skip("PostgreSQL 未启动，跳过知识库集成测试")
    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    _purge(eng)
    yield eng
    _purge(eng)


@pytest.fixture()
def user_id(engine: Engine) -> Iterator[int]:
    """建一个专用测试账号（验证 created_by 落库需要真实的 users.id）。"""
    with Session(engine) as session:
        user = User(
            username=_USERNAME,
            display_name="T21 测试用户",
            dept="研发部",
            role="user",
            status="active",
        )
        session.add(user)
        session.commit()
        yield_uid = user.id
    yield yield_uid
    _purge(engine)


def _kb_name(suffix: str) -> str:
    return f"{_TEST_TAG}-{suffix}"


# ── service 层 ──────────────────────────────────────────────────────
class TestKbService:
    def test_create_returns_contract_shape(
        self, engine: Engine, user_id: int
    ) -> None:
        with Session(engine) as session:
            summary = kb_service.create_kb(
                session,
                name=_kb_name("create"),
                description="描述",
                default_scope="internal",
                created_by=user_id,
            )
        assert summary.id > 0
        assert summary.name == _kb_name("create")
        assert summary.default_scope == "internal"
        assert summary.doc_count == 0  # 新建库必然 0 篇
        assert summary.created_at is not None

    def test_duplicate_name_raises_409(self, engine: Engine) -> None:
        with Session(engine) as session:
            kb_service.create_kb(
                session,
                name=_kb_name("dup"),
                description=None,
                default_scope="internal",
                created_by=None,
            )
        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.create_kb(
                    session,
                    name=_kb_name("dup"),
                    description=None,
                    default_scope="internal",
                    created_by=None,
                )
        assert ei.value.status_code == 409
        assert ei.value.code == "DUPLICATE_KB_NAME"
        # 失败后 session 必须仍可用（说明 rollback 到位，没把事务挂死）
        with Session(engine) as session:
            assert kb_service.list_kbs(session, page=1, page_size=5)[1] >= 1

    @pytest.mark.parametrize("bad_scope", ["all", "PUBLIC", "dept:", "kb:3"])
    def test_invalid_scope_raises_400(
        self, engine: Engine, bad_scope: str
    ) -> None:
        """非法 default_scope 绝不放行：它决定该库后续所有文档的可见范围。"""
        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.create_kb(
                    session,
                    name=_kb_name(f"scope-{bad_scope}"),
                    description=None,
                    default_scope=bad_scope,
                    created_by=None,
                )
        assert ei.value.status_code == 400

    def test_blank_name_raises_400(self, engine: Engine) -> None:
        """全空白名称：pydantic 的 min_length=1 挡不住，服务层必须挡。"""
        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.create_kb(
                    session,
                    name="   ",
                    description=None,
                    default_scope="internal",
                    created_by=None,
                )
        assert ei.value.status_code == 400
        assert "名称" in ei.value.message

    def test_dept_scope_is_allowed(self, engine: Engine) -> None:
        """dept:<部门> 是合法可见范围（T14 白名单），不是只有 internal/public。"""
        with Session(engine) as session:
            summary = kb_service.create_kb(
                session,
                name=_kb_name("dept"),
                description=None,
                default_scope="dept:研发部",
                created_by=None,
            )
        assert summary.default_scope == "dept:研发部"

    def test_get_missing_raises_404(self, engine: Engine) -> None:
        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.get_kb_summary(session, -1)
        assert ei.value.status_code == 404
        assert ei.value.code == "NOT_FOUND"

    def test_update_partial_and_rename_conflict(
        self, engine: Engine
    ) -> None:
        with Session(engine) as session:
            first = kb_service.create_kb(
                session,
                name=_kb_name("upd-a"),
                description=None,
                default_scope="internal",
                created_by=None,
            )
            second = kb_service.create_kb(
                session,
                name=_kb_name("upd-b"),
                description=None,
                default_scope="internal",
                created_by=None,
            )

        with Session(engine) as session:
            updated = kb_service.update_kb(
                session, first.id, description="新描述"
            )
            # 局部更新：没传的字段保持原值
            assert updated.description == "新描述"
            assert updated.name == _kb_name("upd-a")
            assert updated.default_scope == "internal"

        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.update_kb(session, second.id, name=_kb_name("upd-a"))
        assert ei.value.code == "DUPLICATE_KB_NAME"

        with Session(engine) as session:
            with pytest.raises(KbError):
                kb_service.update_kb(session, -1, name="x")

    def test_update_scope_validated(self, engine: Engine) -> None:
        with Session(engine) as session:
            kb = kb_service.create_kb(
                session,
                name=_kb_name("upd-scope"),
                description=None,
                default_scope="internal",
                created_by=None,
            )
        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.update_kb(session, kb.id, default_scope="nope")
        assert ei.value.status_code == 400
        with Session(engine) as session:
            assert kb_service.get_kb_summary(session, kb.id).default_scope == (
                "internal"
            )  # 失败没有写进去

    def test_list_pagination_and_doc_count(self, engine: Engine) -> None:
        with Session(engine) as session:
            for i in range(3):
                kb_service.create_kb(
                    session,
                    name=_kb_name(f"list-{i}"),
                    description=None,
                    default_scope="internal",
                    created_by=None,
                )
            # 给第一个库塞一篇**有效**文档 + 一篇**软删**文档：
            # doc_count 只数有效文档（软删的文档列表里已不存在）
            target = kb_service.create_kb(
                session,
                name=_kb_name("list-count"),
                description=None,
                default_scope="internal",
                created_by=None,
            )
            session.add(
                Document(
                    kb_id=target.id,
                    title="有效文档",
                    source_type="markdown",
                    storage_key=f"{_TEST_TAG}/{_kb_name('count')}/raw.md",
                    scope="internal",
                    status="indexed",
                    is_active=True,
                )
            )
            session.add(
                Document(
                    kb_id=target.id,
                    title="已删文档",
                    source_type="markdown",
                    storage_key=f"{_TEST_TAG}/{_kb_name('count2')}/raw.md",
                    scope="internal",
                    status="indexed",
                    is_active=False,
                )
            )
            session.commit()

        with Session(engine) as session:
            page1, total = kb_service.list_kbs(session, page=1, page_size=2)
            assert len(page1) == 2
            assert total >= 4
            # 顺序 id 倒序：新建的在前，所以 page1 里应有刚建的 count 库
            summary = kb_service.get_kb_summary(session, page1[0].id)
        assert summary.doc_count == 1


class TestKbCascadeDelete:
    """级联删除：清对象 → 删 chunks → 软删 documents → 删库行。"""

    def _seed_kb_with_docs(self, engine: Engine) -> tuple[int, list[str]]:
        with Session(engine) as session:
            kb = kb_service.create_kb(
                session,
                name=_kb_name("cascade"),
                description=None,
                default_scope="internal",
                created_by=None,
            )
            doc = Document(
                kb_id=kb.id,
                title="待级联删除的文档",
                source_type="markdown",
                storage_key=f"{_TEST_TAG}/{_kb_name('cascade')}/raw.md",
                parsed_key=f"{_TEST_TAG}/{_kb_name('cascade')}/parsed.txt",
                scope="internal",
                status="indexed",
                is_active=True,
            )
            session.add(doc)
            session.flush()
            session.add(
                Chunk(
                    doc_id=doc.id,
                    kb_id=kb.id,
                    chunk_index=0,
                    content="待删向量块",
                    scope_key="internal",
                )
            )
            session.commit()
            prefixes = [f"{_TEST_TAG}/{_kb_name('cascade')}/"]
            return kb.id, prefixes

    def test_delete_cascades_all_three_steps(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        kb_id, expected_prefixes = self._seed_kb_with_docs(engine)
        called: list[str] = []
        monkeypatch.setattr(
            kb_service, "delete_prefix", lambda p: called.append(p) or 2
        )

        with Session(engine) as session:
            doc_count = kb_service.delete_kb(
                session, kb_id, scope_keys=["internal"]
            )
        assert doc_count == 1

        # ③ 对象存储：按文档前缀整段删（raw + parsed 一次清理）
        assert called == expected_prefixes

        with Session(engine) as session:
            # ② chunks 真删
            left_chunks = session.scalar(
                select(Chunk).where(Chunk.kb_id == kb_id)
            )
            assert left_chunks is None
            # ① documents 软删（行保留、is_active=false）
            doc = session.scalar(select(Document).where(Document.kb_id == kb_id))
            assert doc is not None
            assert doc.is_active is False
            # ④ 库行真删
            assert session.get(KnowledgeBase, kb_id) is None

    def test_delete_missing_raises_404(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(kb_service, "delete_prefix", lambda p: 0)
        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.delete_kb(session, -1, scope_keys=["internal"])
        assert ei.value.status_code == 404

    def test_delete_is_idempotent_after_success(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重复删除第二次是 404（不是 500）：删除失败的补偿是可重试的。"""
        kb_id, _ = self._seed_kb_with_docs(engine)
        monkeypatch.setattr(kb_service, "delete_prefix", lambda p: 0)
        with Session(engine) as session:
            kb_service.delete_kb(session, kb_id, scope_keys=["internal"])
        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.delete_kb(session, kb_id, scope_keys=["internal"])
        assert ei.value.status_code == 404

    def test_delete_out_of_scope_is_404(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """不在可见范围内的知识库按"不存在"处理（404，不是 403）。

        ★ 口径与 DELETE /documents 统一（OPEN-QUESTIONS #8）：
          `default_scope ∈ scope_keys` 才可见。这是保守方向——
          KB 里可能存在你看不见的文档，看不见就不该删。
        """
        monkeypatch.setattr(kb_service, "delete_prefix", lambda p: 0)
        with Session(engine) as session:
            kb = kb_service.create_kb(
                session,
                name=_kb_name("scoped"),
                description=None,
                default_scope="dept:研发部",
                created_by=None,
            )
            kb_id = kb.id
        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.delete_kb(session, kb_id, scope_keys=["internal"])
            assert ei.value.status_code == 404
            # 未被删除：KB 行还在（对有权者而言仍可管理）
            assert session.get(KnowledgeBase, kb_id) is not None

    def test_delete_kb_axis_mismatch_is_404(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """kb 轴不匹配也 404：scope 可见但 kb_ids 授权不含该 KB。"""
        monkeypatch.setattr(kb_service, "delete_prefix", lambda p: 0)
        with Session(engine) as session:
            kb = kb_service.create_kb(
                session,
                name=_kb_name("kbaxis"),
                description=None,
                default_scope="internal",
                created_by=None,
            )
            kb_id = kb.id
        with Session(engine) as session:
            with pytest.raises(KbError) as ei:
                kb_service.delete_kb(
                    session, kb_id, scope_keys=["internal"], kb_ids=[999999]
                )
            assert ei.value.status_code == 404
            # 对照组：kb_ids 命中时同一用户可以删（排除"恒 404"的假测试）
            kb2 = kb_service.create_kb(
                session,
                name=_kb_name("kbaxis-hit"),
                description=None,
                default_scope="internal",
                created_by=None,
            )
        with Session(engine) as session:
            kb_service.delete_kb(
                session, kb2.id, scope_keys=["internal"], kb_ids=[kb2.id]
            )

    def test_delete_empty_scope_keys_raises(self, engine: Engine) -> None:
        """scope_keys 为空必须显式失败（R2/R7），不得静默放开删除。"""
        with Session(engine) as session:
            with pytest.raises(ValueError, match="scope_keys"):
                kb_service.delete_kb(session, 1, scope_keys=[])


# ── api 层 ──────────────────────────────────────────────────────────
class TestKbApi:
    @pytest.fixture()
    def client(self, engine: Engine, user_id: int) -> Iterator[TestClient]:
        eng = engine
        uid = user_id
        prefixes: list[str] = []

        def _override_session() -> Iterator[Session]:
            with Session(eng) as session:
                yield session

        app.dependency_overrides[get_session] = _override_session
        app.dependency_overrides[_require_user_permission] = lambda: (
            PermissionBundle(scope_keys=["internal"], user_id=uid)
        )
        # 对象存储不打真实服务（级联删除会按前缀清对象）
        import app.services.kb as kb_mod

        real_delete_prefix = kb_mod.delete_prefix
        kb_mod.delete_prefix = lambda p: prefixes.append(p) or 0  # type: ignore[assignment]
        try:
            with TestClient(app) as c:
                c.deleted_prefixes = prefixes  # type: ignore[attr-defined]
                yield c
        finally:
            app.dependency_overrides.clear()
            kb_mod.delete_prefix = real_delete_prefix  # type: ignore[assignment]

    def test_create_201_and_created_by(
        self, client: TestClient, engine: Engine, user_id: int
    ) -> None:
        resp = client.post(
            "/api/v1/knowledge-bases",
            json={"name": _kb_name("api-create"), "description": "接口创建"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        # 字段形状严格按 openapi KnowledgeBase
        assert set(body) == {
            "id",
            "name",
            "description",
            "default_scope",
            "doc_count",
            "created_at",
        }
        assert body["default_scope"] == "internal"  # 不传时的契约默认值
        assert body["doc_count"] == 0

        with Session(engine) as session:
            kb = session.get(KnowledgeBase, body["id"])
            assert kb is not None
            assert kb.created_by == user_id  # 身份 → created_by 接线

    def test_create_duplicate_409(self, client: TestClient) -> None:
        payload = {"name": _kb_name("api-dup")}
        assert client.post("/api/v1/knowledge-bases", json=payload).status_code == 201
        resp = client.post("/api/v1/knowledge-bases", json=payload)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "DUPLICATE_KB_NAME"

    def test_create_invalid_scope_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/knowledge-bases",
            json={"name": _kb_name("api-bad-scope"), "default_scope": "all"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "QUESTION_INVALID"

    def test_create_blank_name_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/knowledge-bases", json={"name": "   "}
        )
        assert resp.status_code == 400

    def test_create_missing_name_400(self, client: TestClient) -> None:
        """缺字段走 FastAPI 校验 → 400；文案不能是 /query 的"问题参数不合法"。"""
        resp = client.post("/api/v1/knowledge-bases", json={})
        assert resp.status_code == 400
        message = resp.json()["error"]["message"]
        assert "问题参数" not in message

    def test_create_overlong_name_400(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/knowledge-bases", json={"name": "x" * 101}
        )
        assert resp.status_code == 400

    def test_list_envelope_and_paging_guard(self, client: TestClient) -> None:
        client.post("/api/v1/knowledge-bases", json={"name": _kb_name("api-list")})
        resp = client.get("/api/v1/knowledge-bases", params={"page": 1, "page_size": 5})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"items", "total", "page", "page_size"}
        assert body["page"] == 1 and body["page_size"] == 5
        assert body["total"] >= 1
        assert all(set(i) >= {"id", "name", "doc_count"} for i in body["items"])

        # 参数越界：page<1 与 page_size>100 都必须挡（否则一次拉全库）
        assert (
            client.get("/api/v1/knowledge-bases", params={"page": 0}).status_code
            == 400
        )
        assert (
            client.get(
                "/api/v1/knowledge-bases", params={"page_size": 101}
            ).status_code
            == 400
        )

    def test_patch_200_and_404(self, client: TestClient) -> None:
        kb_id = client.post(
            "/api/v1/knowledge-bases", json={"name": _kb_name("api-patch")}
        ).json()["id"]
        resp = client.patch(
            f"/api/v1/knowledge-bases/{kb_id}",
            json={"description": "改后的描述", "default_scope": "public"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "改后的描述"
        assert resp.json()["default_scope"] == "public"
        assert (resp.json()["name"]) == _kb_name("api-patch")  # 未传不改

        missing = client.patch(
            "/api/v1/knowledge-bases/999999999", json={"name": "whatever"}
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"

    def test_delete_204_and_404(self, client: TestClient) -> None:
        kb_id = client.post(
            "/api/v1/knowledge-bases", json={"name": _kb_name("api-del")}
        ).json()["id"]
        resp = client.delete(f"/api/v1/knowledge-bases/{kb_id}")
        assert resp.status_code == 204
        assert resp.content == b""
        # 删两次：第二次 404
        assert (
            client.delete(f"/api/v1/knowledge-bases/{kb_id}").status_code == 404
        )

    def test_delete_out_of_scope_via_api_is_404(
        self, client: TestClient
    ) -> None:
        """API 层越权删除：default_scope 不在用户 scope_keys 内 → 404。

        ★ client 的身份 override 是 scope_keys=["internal"]（见 fixture），
          建一个 dept: 知识库再删，应按"不存在"处理。
        """
        kb_id = client.post(
            "/api/v1/knowledge-bases",
            json={"name": _kb_name("api-del-oos"), "default_scope": "dept:财务"},
        ).json()["id"]
        resp = client.delete(f"/api/v1/knowledge-bases/{kb_id}")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "NOT_FOUND"

    def test_requires_auth(self, engine: Engine) -> None:
        """不覆盖依赖、不带 X-KB-User → 401（列表也不能匿名拉）。"""
        app.dependency_overrides.clear()
        try:
            with TestClient(app) as c:
                resp = c.get("/api/v1/knowledge-bases")
            assert resp.status_code == 401
            assert resp.json()["error"]["code"] == "UNAUTHENTICATED"
        finally:
            app.dependency_overrides.clear()
