"""知识库数据访问（V0.1 · T21）。

★ 目录约定：这是唯一允许出现 SQL 的层之一（app/repos/），
  服务层不写 SQL，接口层不查库。
★ 事务边界：本模块的方法**只 flush、不 commit**，由 services/kb.py 统一控事务
  （与 task_repo 相反——那里 insert_task 自带 commit，是因为入队后 worker
  立刻来读、必须先落库；知识库 CRUD 是一问一答，整体成功或整体失败）。
"""

from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.repos.models import Chunk, Document, KnowledgeBase


def list_kbs_with_counts(
    session: Session, *, page: int, page_size: int
) -> tuple[list[tuple[KnowledgeBase, int]], int]:
    """分页列出知识库，并带上各自的**有效文档数**。

    Returns:
        ([(知识库, 有效文档数)], 总条数)。
        排序：id 倒序（新库在前）。契约未规定顺序，取"新建的可见"这一最直观的默认。

    ★ doc_count 只数 `is_active=true` 的文档：删除知识库时文档是软删
      （data-model 第四节），软删的文档不该再出现在计数里。
    ★ 用 LEFT JOIN 聚合子查询而不是每行单独 COUNT：一次 SQL 拿全，
      避免 N+1（列表页 N 次查询）。
    """
    counts = (
        select(
            Document.kb_id.label("kb_id"),
            func.count(Document.id).label("doc_count"),
        )
        .where(Document.is_active.is_(True))
        .group_by(Document.kb_id)
        .subquery()
    )
    total = session.scalar(select(func.count(KnowledgeBase.id))) or 0
    rows = session.execute(
        select(KnowledgeBase, func.coalesce(counts.c.doc_count, 0))
        .outerjoin(counts, counts.c.kb_id == KnowledgeBase.id)
        .order_by(KnowledgeBase.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [(kb, int(cnt)) for kb, cnt in rows], int(total)


def count_active_docs(session: Session, *, kb_id: int) -> int:
    """某知识库的有效文档数（详情/创建/更新响应用的 doc_count）。"""
    return int(
        session.scalar(
            select(func.count(Document.id)).where(
                Document.kb_id == kb_id, Document.is_active.is_(True)
            )
        )
        or 0
    )


def get_kb(session: Session, kb_id: int) -> KnowledgeBase | None:
    """按 id 取知识库；不存在返回 None（由服务层翻译成 404）。"""
    return session.get(KnowledgeBase, kb_id)


def find_kb_by_name(session: Session, *, name: str) -> KnowledgeBase | None:
    """按名称精确取（判重名用）。"""
    return session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))


def insert_kb(
    session: Session,
    *,
    name: str,
    description: str | None,
    default_scope: str,
    created_by: int | None,
) -> KnowledgeBase:
    """插入知识库并 flush（拿自增 id，不 commit）。

    ★ 重名不做应用层拦截：name 上的 UNIQUE 约束是唯一可靠防线
      （先 SELECT 再 INSERT 存在竞态）。调用方需自行捕获 IntegrityError
      翻译成 409 DUPLICATE_KB_NAME——见 services/kb.py。
    """
    kb = KnowledgeBase(
        name=name,
        description=description,
        default_scope=default_scope,
        created_by=created_by,
    )
    session.add(kb)
    session.flush()
    return kb


def update_kb(
    session: Session,
    kb_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    default_scope: str | None = None,
) -> None:
    """局部更新（值为 None 表示"本次不改这一列"，不清空已有值）。

    ★ description 无法被"更新为空"：契约的 PATCH 用 None 表示"不改"，
      且 V0.1 没有"清空描述"的需求，故不做 `description=""` → NULL 的转换。
    """
    values: dict[str, object] = {}
    if name is not None:
        values["name"] = name
    if description is not None:
        values["description"] = description
    if default_scope is not None:
        values["default_scope"] = default_scope
    if not values:
        return
    session.execute(
        update(KnowledgeBase).where(KnowledgeBase.id == kb_id).values(**values)
    )
    session.flush()


def list_doc_storage_prefixes(session: Session, *, kb_id: int) -> list[str]:
    """该知识库下**所有**文档在对象存储里的前缀 `docs/{doc_uuid}/`（含已软删的）。

    ★ 返回前缀而非具体 key：一个文档前缀下可能同时有 `raw.{ext}` 与
      `parsed.txt`，也可能只有其中之一（解析失败的文档没有 parsed_key）。
      按前缀整段删既能覆盖两种情况，也不依赖"DB 里记全了每个对象"这一假设
      ——storage.py 注释里写的 V1 全链路清理（T47）就是按前缀删。
    ★ 含已软删的文档：软删只改 DB 标记、对象还在存储里；只看 active 的会漏掉
      早先软删文档的对象（内容仍留在对象存储 = 静默的数据留存）。
    """
    rows = session.execute(
        select(Document.storage_key, Document.parsed_key).where(
            Document.kb_id == kb_id
        )
    ).all()
    prefixes: set[str] = set()
    for storage_key, parsed_key in rows:
        for key in (storage_key, parsed_key):
            if key and "/" in key:
                prefixes.add(key.rsplit("/", 1)[0] + "/")
    return sorted(prefixes)


def delete_kb_cascade(session: Session, *, kb_id: int) -> int:
    """级联删除（T21 定档的"三步"），返回被软删的文档数。

    ① 真删 chunks（含向量）——"以为删干净了其实没有"是本项目最不能接受的
       形态，故 chunks 不留软删；
    ② documents 置 is_active=false（data-model 第四节：列表立即消失，
       行保留以支持"谁删了什么"的追溯）；
    ③ 真删 knowledge_bases 行。

    ★ 不含 data-model 第四节的第 4—7 步（缓存失效 / 评估题 stale 标记 /
      审计日志），它们均超出 V0.1 范围（V0.1 明确不做审计日志）。
    ★ 对象存储的清理**不在这里**：SQL 层不碰外部服务，由 services/kb.py
      在调用本方法**之前**完成（先删对象、后删库，理由见服务层注释）。
    """
    doc_count = session.scalar(
        select(func.count(Document.id)).where(
            Document.kb_id == kb_id, Document.is_active.is_(True)
        )
    )
    session.execute(delete(Chunk).where(Chunk.kb_id == kb_id))
    session.execute(
        update(Document)
        .where(Document.kb_id == kb_id, Document.is_active.is_(True))
        .values(is_active=False)
    )
    session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    session.flush()
    return int(doc_count or 0)
