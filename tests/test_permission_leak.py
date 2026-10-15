"""T17 越权回归测试集（≥ 4 条）。

验收标准（tasks.md T17）：`make test T="-k permission"` 全绿。

与 T14（解析逻辑）、T15（单条越权验收）的区别：
- T14 测 `document_scope_key` / `user_scope_keys` 的**计算逻辑**（纯函数 / 轻量查库）；
- T15 测"销售部看不到研发部"这**一条**核心路径（依赖 Ollama 嵌入）；
- 本文件：**多条回归 case 覆盖不同越权形态**，每条负例配正例对照（防"啥都没查到"
  让断言恒真），**不依赖 Ollama**（用确定性 one-hot 向量，pgvector 余弦距离 = 距离 0
  → 相似度 1.0 → 目标块必排第一），只需要 PostgreSQL + pgvector。

为什么不用 Ollama：Ollama 不可达时 T15 会整片 skip → "越权门禁存在假绿风险"
（quality-gates H6 硬门禁，不能靠 skip 蒙混）。这里所有断言只需要"目标块能被召回"
这一个前提，one-hot 向量就能稳定提供。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.repos.chunk_repo import search_chunks
from app.repos.models import Chunk, Document, User, UserScopeGrant
from app.services.permission import document_scope_key, resolve_scope_keys

# ── 测试常量 ──────────────────────────────────────────────
# 专用账号 / 文档标题前缀：避免与 T14/T15 或演示数据混淆
_SALES_USER = "t17_sales"
_RND_USER = "t17_rnd"
_NO_DEPT_USER = "t17_nodept"

# 4 个文档 + 各自的 scope_key 与 one-hot 向量的"热点"位置
# 每个热点位置唯一 → 查询向量 = 热点向量 → 余弦距离 0 → 必排第一
# VECTOR_DIM 必须等于 pgvector 列的维度（models.py Chunk.embedding: Vector(1024)）
_VECTOR_DIM = 1024

def _hot_vector(hot_index: int) -> list[float]:
    """造一个 one-hot 向量：hot_index 位为 1.0，其余 0.0。

    查询这个向量时，任何其他文档（热点位不同）的余弦距离都是固定值
    （远大于 0），目标块必排第一。
    """
    v = [0.0] * _VECTOR_DIM
    v[hot_index] = 1.0
    return v

# (文档标题, scope, 热点位置, 内容提示, kb_id)
_DOC_SPECS: list[tuple[str, str, int, str, int]] = [
    ("T17-销售部文档", "dept:销售部", 0, "销售部专属文档", 1),
    ("T17-研发部文档", "dept:研发部", 1, "研发部专属文档", 1),
    ("T17-财务部文档", "dept:财务部", 2, "财务部专属文档", 1),
    ("T17-公开文档",   "public",      3, "全员可见的公开文档", 1),
    # T17b kb 轴：同 scope_key(internal) 不同 kb_id，热点 4/5
    ("T17-kb1内部文档", "internal",   4, "kb1 的内部文档", 1),
    ("T17-kb2内部文档", "internal",   5, "kb2 的内部文档", 2),
]

# 需要预置授权的场景（授权变更 case 会动态加/删）
_SALES_GRANTS: list[str] = []            # 销售部：本部门自动可见
_RND_GRANTS: list[str] = []              # 研发部：同上
_NODEPT_GRANTS: list[str] = ["dept:销售部"]


# ── 环境检查与 fixture ───────────────────────────────────
def _db_available() -> bool:
    import os

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


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="PostgreSQL 未启动，跳过越权回归测试（T17）",
)


@pytest.fixture()
def engine() -> Iterator[Engine]:
    import os

    eng = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    _purge_all(eng)
    yield eng
    _purge_all(eng)


def _purge_all(engine: Engine) -> None:
    """清掉本测试所有可能遗留的数据。幂等，可重复跑。"""
    with Session(engine) as session:
        doc_titles = [s[0] for s in _DOC_SPECS]
        doc_ids = session.scalars(
            select(Document.id).where(Document.title.in_(doc_titles))
        ).all()
        if doc_ids:
            session.execute(delete(Chunk).where(Chunk.doc_id.in_(doc_ids)))
            session.execute(delete(Document).where(Document.id.in_(doc_ids)))
        user_ids = session.scalars(
            select(User.id).where(
                User.username.in_([_SALES_USER, _RND_USER, _NO_DEPT_USER])
            )
        ).all()
        if user_ids:
            session.execute(
                delete(UserScopeGrant).where(UserScopeGrant.user_id.in_(user_ids))
            )
            session.execute(delete(User).where(User.id.in_(user_ids)))
        session.commit()


@pytest.fixture()
def seed(engine: Engine) -> dict[str, int]:
    """造好 3 个用户 + 4 个文档 + 4 个 chunk；返回 {title: doc_id} 供用例引用。

    所有向量用确定性 one-hot（热点位 0—3 各占一个），不依赖 Ollama。
    """
    with Session(engine) as session:
        _purge_all(engine)

        session.add_all(
            [
                User(
                    username=_SALES_USER, display_name="T17-销售测试",
                    dept="销售部", role="user", status="active",
                ),
                User(
                    username=_RND_USER, display_name="T17-研发测试",
                    dept="研发部", role="user", status="active",
                ),
                User(
                    username=_NO_DEPT_USER, display_name="T17-无部门测试",
                    dept=None, role="user", status="active",
                ),
            ]
        )
        session.flush()
        # 预置无部门用户的授权（其它用户靠 dept 自动可见）
        no_dept_uid = session.scalar(
            select(User.id).where(User.username == _NO_DEPT_USER)
        )
        assert no_dept_uid is not None
        session.add(UserScopeGrant(user_id=no_dept_uid, scope_key=_NODEPT_GRANTS[0]))
        session.commit()

        doc_ids: dict[str, int] = {}
        chunks_to_add: list[Chunk] = []

        for title, scope, hot, content, kb_id in _DOC_SPECS:
            doc = Document(
                title=title, source_type="txt", scope=scope, status="done",
                kb_id=kb_id,
            )
            session.add(doc)
            session.flush()
            doc_ids[title] = doc.id

            chunks_to_add.append(
                Chunk(
                    doc_id=doc.id,
                    kb_id=kb_id,
                    chunk_index=0,
                    content=content,
                    char_start=0,
                    char_end=len(content),
                    embedding=_hot_vector(hot),
                    scope_key=document_scope_key(scope),
                )
            )

        session.add_all(chunks_to_add)
        session.commit()

    return doc_ids


# ── Case 1：部门隔离 ─────────────────────────────────────
def test_dept_isolation_sales_cannot_see_rnd(
    engine: Engine, seed: dict[str, int],
) -> None:
    """销售部看不到研发部；研发部能看到研发部（正例对照）。

    正例对照的意义：如果只断言"没出现研发部"，那"啥都没查到"会让断言恒真。
    必须同时断言"该看到时看得到"，才能证明是权限过滤把它排除了，不是碰巧。
    """
    with Session(engine) as session:
        sales_keys = resolve_scope_keys(session, username=_SALES_USER)
        rnd_keys = resolve_scope_keys(session, username=_RND_USER)

    # 前置：范围解析正确
    assert "dept:销售部" in sales_keys
    assert "dept:研发部" not in sales_keys
    assert "dept:研发部" in rnd_keys

    rnd_doc_id = seed["T17-研发部文档"]
    rnd_query_vec = _hot_vector(1)  # = 研发部文档向量 → 余弦距离 0

    # 正例对照：研发部账号应该能召回它
    rnd_hits = search_chunks(rnd_query_vec, rnd_keys, top_k=5, engine=engine)
    assert any(h.doc_id == rnd_doc_id for h in rnd_hits), (
        "研发部账号应能召回研发部文档；否则无法证明是权限过滤"
        "（而非该文档分数太低或 SQL 根本没查）"
    )

    # 负例：销售部账号应该完全看不到它
    sales_hits = search_chunks(rnd_query_vec, sales_keys, top_k=5, engine=engine)
    leaked = [h.doc_id for h in sales_hits if h.doc_id == rnd_doc_id]
    assert leaked == [], (
        f"销售部账号检索到研发部 doc：{leaked}（R2 被违反：权限过滤未在 SQL 内生效）"
    )


# ── Case 2：无部门用户 + 只授权销售部 ────────────────────
def test_no_dept_user_cannot_see_other_depts(
    engine: Engine, seed: dict[str, int],
) -> None:
    """无部门用户 dept=None，只授权 dept:销售部。
    能看到销售部（正例）+ 看不到研发部/财务部（负例）+ 能看到 public（正例）。

    这条防"过滤过头"——scope_key = ANY([dept:销售部, public, internal])
    按设计应该同时命中销售部文档和 public 文档。如果 public 也被排除了，
    说明 SQL 里的条件不是 ANY，而是 == 或被意外 AND 了别的条件。
    """
    with Session(engine) as session:
        keys = resolve_scope_keys(session, username=_NO_DEPT_USER)

    # 前置：范围解析正确
    assert keys == ["dept:销售部", "internal", "public"]

    sales_doc_id = seed["T17-销售部文档"]
    rnd_doc_id = seed["T17-研发部文档"]
    finance_doc_id = seed["T17-财务部文档"]
    public_doc_id = seed["T17-公开文档"]

    # 正例 1：能看到销售部
    sales_hits = search_chunks(_hot_vector(0), keys, top_k=5, engine=engine)
    assert any(h.doc_id == sales_doc_id for h in sales_hits)

    # 负例：看不到研发部 / 财务部
    rnd_hits = search_chunks(_hot_vector(1), keys, top_k=5, engine=engine)
    assert not any(h.doc_id == rnd_doc_id for h in rnd_hits)

    finance_hits = search_chunks(_hot_vector(2), keys, top_k=5, engine=engine)
    assert not any(h.doc_id == finance_doc_id for h in finance_hits)

    # 正例 2：能看到 public 文档（过滤过头的保险丝）
    public_hits = search_chunks(_hot_vector(3), keys, top_k=5, engine=engine)
    assert any(h.doc_id == public_doc_id for h in public_hits)


# ── Case 3：授权变更即时生效 ──────────────────────────────
def test_grant_change_takes_effect_immediately(
    engine: Engine, seed: dict[str, int],
) -> None:
    """无部门用户 + 初始只授权 dept:销售部（fixture 已预置）。

    三次调用 search_chunks：
    1. 初始：看不到研发部
    2. 加授权 dept:研发部 → 立即可见
    3. 撤销 dept:研发部 → 立刻又看不到

    防"权限被缓存 / 固化在 session 里"——如果 SQL 没有实时 JOIN 授权表，
    加了授权也看不到。
    """
    # 前置：初始状态只有 dept:销售部
    with Session(engine) as session:
        initial_keys = resolve_scope_keys(session, username=_NO_DEPT_USER)
    assert "dept:研发部" not in initial_keys

    rnd_doc_id = seed["T17-研发部文档"]
    rnd_query = _hot_vector(1)

    # 1. 初始：看不到研发部
    hits = search_chunks(rnd_query, initial_keys, top_k=5, engine=engine)
    assert not any(h.doc_id == rnd_doc_id for h in hits), (
        "初始状态下不应看到研发部文档"
    )

    # 2. 加授权 → 解析新范围 → 应该能看到
    with Session(engine) as session:
        user = session.scalar(
            select(User).where(User.username == _NO_DEPT_USER)
        )
        assert user is not None
        session.add(UserScopeGrant(user_id=user.id, scope_key="dept:研发部"))
        session.commit()
    with Session(engine) as session:
        new_keys = resolve_scope_keys(session, username=_NO_DEPT_USER)
    assert "dept:研发部" in new_keys

    hits = search_chunks(rnd_query, new_keys, top_k=5, engine=engine)
    assert any(h.doc_id == rnd_doc_id for h in hits), (
        "加了 dept:研发部 授权后应该可见"
    )

    # 3. 撤销授权 → 又看不到
    with Session(engine) as session:
        user = session.scalar(
            select(User).where(User.username == _NO_DEPT_USER)
        )
        assert user is not None
        session.execute(
            delete(UserScopeGrant).where(
                UserScopeGrant.user_id == user.id,
                UserScopeGrant.scope_key == "dept:研发部",
            )
        )
        session.commit()
    with Session(engine) as session:
        reverted_keys = resolve_scope_keys(session, username=_NO_DEPT_USER)
    assert "dept:研发部" not in reverted_keys

    hits = search_chunks(rnd_query, reverted_keys, top_k=5, engine=engine)
    assert not any(h.doc_id == rnd_doc_id for h in hits), (
        "撤销 dept:研发部 授权后应不可见"
    )


# ── Case 4：点状授权不得旁及 ──────────────────────────────
def test_single_dept_grant_does_not_bleed(
    engine: Engine, seed: dict[str, int],
) -> None:
    """销售部用户（本部门自动可见）+ 额外授权 dept:研发部。
    能看到销售部 + 研发部（正例）+ 看不到财务部（负例）。

    防"SQL 条件写成了 `scope_key LIKE 'dept:%'` 或被通配符匹配"——
    只给 dept:研发部 的授权，绝不能让 dept:财务部 也溜进来。
    """
    # 给销售部用户加额外授权
    with Session(engine) as session:
        user = session.scalar(
            select(User).where(User.username == _SALES_USER)
        )
        assert user is not None
        session.add(UserScopeGrant(user_id=user.id, scope_key="dept:研发部"))
        session.commit()

    with Session(engine) as session:
        keys = resolve_scope_keys(session, username=_SALES_USER)

    # 前置：解析正确 — 销售部（本部门）+ 研发部（授权）+ public/internal
    assert "dept:销售部" in keys
    assert "dept:研发部" in keys
    assert "dept:财务部" not in keys

    sales_doc_id = seed["T17-销售部文档"]
    rnd_doc_id = seed["T17-研发部文档"]
    finance_doc_id = seed["T17-财务部文档"]

    # 正例：销售部 + 研发部都应该可见
    assert any(
        h.doc_id == sales_doc_id
        for h in search_chunks(_hot_vector(0), keys, top_k=5, engine=engine)
    )
    assert any(
        h.doc_id == rnd_doc_id
        for h in search_chunks(_hot_vector(1), keys, top_k=5, engine=engine)
    )

    # 负例：财务部绝不能出现
    finance_hits = search_chunks(_hot_vector(2), keys, top_k=5, engine=engine)
    assert not any(h.doc_id == finance_doc_id for h in finance_hits), (
        "点状授权旁及：只给了 dept:研发部，却看到了 dept:财务部"
        "（scope_key 匹配被绕过）"
    )


# ── Case 5：R7 空权限必须显式失败 ─────────────────────────
def test_search_chunks_rejects_empty_scope_keys(
    engine: Engine, seed: dict[str, int],
) -> None:
    """`search_chunks(vec, [])` 必须抛 ValueError。

    R7 显式失败：宁可抛错也不返回"查全库"的结果（空列表不是兜底，是隐患）。
    这条是最后一道防线 —— 如果哪次改动忘了传 scope_keys，它必须在测试里炸出来。
    """
    with pytest.raises(ValueError, match="scope_keys 不能为空"):
        search_chunks(_hot_vector(0), [], top_k=5, engine=engine)


# ── Case 6：T17b kb 轴隔离（两条件 SQL 的第二条件）─────────────────
def test_kb_axis_isolation_internal_docs(
    engine: Engine, seed: dict[str, int],
) -> None:
    """T17b 验收：授权 internal + kb:1 的用户看不到 kb:2 的 internal 文档。

    两个文档 scope_key 都是 internal —— scope_key 维度**拦不住**，
    能拦住它们的只有第二条件 `c.kb_id = ANY(:kb_ids)`。
    这条专门验证 kb 轴 SQL 条件真的生效，而不是形同虚设。
    """
    kb1_doc_id = seed["T17-kb1内部文档"]
    kb2_doc_id = seed["T17-kb2内部文档"]
    scope_keys = ["internal"]  # 两文档 scope_key 都命中

    # 正例 1：kb_ids=[1] → 能看到 kb1 文档（两条件都满足）
    hits = search_chunks(_hot_vector(4), scope_keys, top_k=5, kb_ids=[1], engine=engine)
    assert any(h.doc_id == kb1_doc_id for h in hits), (
        "kb_ids=[1] 时应能召回 kb1 文档；否则无法证明是 kb 过滤而非分数问题"
    )

    # 负例：kb_ids=[1] → 看不到 kb2 文档（scope_key 命中但 kb_id 不命中）
    hits = search_chunks(_hot_vector(5), scope_keys, top_k=5, kb_ids=[1], engine=engine)
    leaked = [h.doc_id for h in hits if h.doc_id == kb2_doc_id]
    assert leaked == [], (
        f"kb_ids=[1] 却检索到 kb2 的 internal 文档：{leaked}"
        "（kb_id 轴条件未生效，T17b 验收不通过）"
    )

    # 正例 2：不传 kb_ids → kb 条件不追加 → kb2 文档可见（证明上面确实是
    # kb 条件拦的，不是别的什么把它过滤掉了）
    hits = search_chunks(_hot_vector(5), scope_keys, top_k=5, engine=engine)
    assert any(h.doc_id == kb2_doc_id for h in hits), (
        "不传 kb_ids 时 kb2 文档应可见（kb 条件不追加）；"
        "否则负例的'看不到'可能是别的原因造成的假隔离"
    )
