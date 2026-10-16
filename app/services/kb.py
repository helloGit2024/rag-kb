"""知识库业务编排（V0.1 · T21）。

职责：校验（名称 / default_scope）→ 调 repo → 控事务 → 级联删除时协调对象存储。
★ 不写 SQL（在 app/repos/kb_repo.py）；不产 HTTP 响应（在 app/api/）。

T21 定档的三个决策（2026-10-16 与项目所有者确认）：
1. **级联删除走"三步"**：真删 chunks + 文档软删 + 删知识库行；不做 data-model
   第四节的第 4—7 步（缓存失效 / 评估题 stale / 审计日志——均超 V0.1 范围）。
2. **重名返回 409 DUPLICATE_KB_NAME**：契约错误码清单原本没有该码，
   经确认后补入 api-contract.md（与 DUPLICATE_FILE / DUPLICATE_MODEL_NAME 同族）。
3. **列表不按权限过滤**：V0.1 无登录/RBAC，知识库只是容器；真正的越权防线在
   chunks 的 (scope_key, kb_id) 两条件 SQL 上（T17 / T17b 回归覆盖）。
   列表仍要求身份合法（401 由 api 依赖挡住）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.storage import delete_prefix
from app.repos import kb_repo
from app.repos.models import KnowledgeBase
from app.services.permission import ScopeError, document_scope_key

# 契约长度上限（openapi.yaml：name maxLength 100 / description maxLength 500）
MAX_NAME_LEN = 100
MAX_DESCRIPTION_LEN = 500


class KbError(Exception):
    """知识库业务异常（带结构化错误码）。

    字段与 app/services/file_validator.py 的 FileValidationError 对齐——
    服务层不 import api 层的 ApiError，由 main.py 的处理器统一转错误信封。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        detail: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class KbSummary:
    """知识库的可返回视图（与 openapi KnowledgeBase schema 字段一致）。

    为什么不用 ORM 对象当返回值：session.commit() 后 ORM 属性会过期，
    在服务边界之外读它等于隐式再发一次 SELECT；先在服务层固化取值，
    接口层拿到的就是纯数据（也顺手把"返回什么字段"收在一处）。
    """

    id: int
    name: str
    description: str | None
    default_scope: str
    doc_count: int
    created_at: datetime


def _to_summary(kb: KnowledgeBase, doc_count: int) -> KbSummary:
    return KbSummary(
        id=kb.id,
        name=kb.name,
        description=kb.description,
        default_scope=kb.default_scope,
        doc_count=doc_count,
        created_at=kb.created_at,
    )


def _validate_name(name: str) -> str:
    """名称规整 + 校验。

    ★ 为什么服务层还要再校验一遍（pydantic 已限 1—100 字）：
      pydantic 的 min_length=1 挡不住"全是空白"的名称，而空白名称在列表里
      和"空行"没有区别，属于数据质量问题。这里 strip 后判空。
    """
    value = (name or "").strip()
    if not value:
        # 契约 400 里只有 QUESTION_INVALID（语义偏 query）可用，
        # 与 documents.py 处理非法 scope 时同一处妥协，已记入 OPEN-QUESTIONS，
        # 待契约补通用参数码后统一替换。
        raise KbError(400, "QUESTION_INVALID", "知识库名称不能为空")
    if len(value) > MAX_NAME_LEN:
        raise KbError(
            400,
            "QUESTION_INVALID",
            f"知识库名称超过 {MAX_NAME_LEN} 字上限",
            {"max_length": MAX_NAME_LEN, "actual": len(value)},
        )
    return value


def _validate_scope(scope: str) -> str:
    """default_scope 必须落在 T14 的 scope 白名单内（同一处白名单，不另写一套）。

    Raises:
        KbError: 400 —— 非法取值绝不放行；知识库默认 scope 写错会污染
            其后所有上传文档的可见范围（SecurityError 类问题）。
    """
    try:
        return document_scope_key(scope)
    except ScopeError as exc:
        raise KbError(400, "QUESTION_INVALID", str(exc)) from exc


def _validate_description(description: str | None) -> str | None:
    if description is None:
        return None
    if len(description) > MAX_DESCRIPTION_LEN:
        raise KbError(
            400,
            "QUESTION_INVALID",
            f"描述超过 {MAX_DESCRIPTION_LEN} 字上限",
            {"max_length": MAX_DESCRIPTION_LEN, "actual": len(description)},
        )
    return description


def list_kbs(
    session: Session, *, page: int, page_size: int
) -> tuple[list[KbSummary], int]:
    """分页列出知识库（含各自有效文档数）。"""
    rows, total = kb_repo.list_kbs_with_counts(
        session, page=page, page_size=page_size
    )
    return [_to_summary(kb, cnt) for kb, cnt in rows], total


def get_kb_summary(session: Session, kb_id: int) -> KbSummary:
    """取单个知识库（含有效文档数）。

    Raises:
        KbError: 404 NOT_FOUND。
    """
    kb = kb_repo.get_kb(session, kb_id)
    if kb is None:
        raise KbError(
            404, "NOT_FOUND", "知识库不存在", {"kb_id": kb_id}
        )
    doc_count = kb_repo.count_active_docs(session, kb_id=kb_id)
    return _to_summary(kb, doc_count)


def create_kb(
    session: Session,
    *,
    name: str,
    description: str | None,
    default_scope: str,
    created_by: int | None,
) -> KbSummary:
    """创建知识库。

    重名判定**以数据库 UNIQUE 约束为准**（R7：唯一的可靠防线）。
    先 SELECT 再 INSERT 存在竞态——两个并发请求可以同时通过检查；
    因此这里不做"预检查 + 相信它"的写法，而是直接插入并捕获 IntegrityError。

    Raises:
        KbError: 400（名称/描述/scope 非法）、409 DUPLICATE_KB_NAME（重名）。
    """
    clean_name = _validate_name(name)
    clean_desc = _validate_description(description)
    clean_scope = _validate_scope(default_scope)
    try:
        kb = kb_repo.insert_kb(
            session,
            name=clean_name,
            description=clean_desc,
            default_scope=clean_scope,
            created_by=created_by,
        )
        # flush 阶段属性已填充，先固化成 summary 再 commit（commit 后属性会过期）
        summary = _to_summary(kb, doc_count=0)
        session.commit()
    except IntegrityError as exc:
        # 事务已废，必须先 rollback 才能让 session 继续可用
        session.rollback()
        raise KbError(
            409,
            "DUPLICATE_KB_NAME",
            "知识库名称已存在",
            {"name": clean_name},
        ) from exc
    return summary


def update_kb(
    session: Session,
    kb_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    default_scope: str | None = None,
) -> KbSummary:
    """局部更新知识库（None = 本次不改该字段）。

    Raises:
        KbError: 404 NOT_FOUND、400（入参非法）、409 DUPLICATE_KB_NAME（改名撞车）。
    """
    kb = kb_repo.get_kb(session, kb_id)
    if kb is None:
        raise KbError(404, "NOT_FOUND", "知识库不存在", {"kb_id": kb_id})

    clean_name = _validate_name(name) if name is not None else None
    clean_desc = (
        _validate_description(description) if description is not None else None
    )
    clean_scope = (
        _validate_scope(default_scope) if default_scope is not None else None
    )

    try:
        kb_repo.update_kb(
            session,
            kb_id,
            name=clean_name,
            description=clean_desc,
            default_scope=clean_scope,
        )
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise KbError(
            409,
            "DUPLICATE_KB_NAME",
            "知识库名称已存在",
            {"name": clean_name},
        ) from exc

    # commit 后 identity map 里的对象已过期，重新取一次拿到最新值
    refreshed = kb_repo.get_kb(session, kb_id)
    if refreshed is None:  # pragma: no cover - 同事务内刚更新过，不可能消失
        raise KbError(404, "NOT_FOUND", "知识库不存在", {"kb_id": kb_id})
    doc_count = kb_repo.count_active_docs(session, kb_id=kb_id)
    return _to_summary(refreshed, doc_count)


def delete_kb(session: Session, kb_id: int) -> int:
    """删除知识库（级联：清对象 → 删向量 → 软删文档 → 删库行）。

    Returns:
        被软删的文档数（供日志/排查用）。

    Raises:
        KbError: 404 NOT_FOUND。对象存储故障（S3Error）原样上抛 → 500（R7）。

    ★ 顺序是"先删对象、后删库行"，不是反过来：反过来的话，若对象删除失败，
      知识库行已经没了，用户重试只会得到 404，而原文就**永久留在对象存储里**
      ——静默的数据留存，且没有任何机制能发现。反过来（先对象后库行）最坏
      结果是"对象已清、库行还在"，用户重试即可收敛（删除是幂等的）。
    """
    kb = kb_repo.get_kb(session, kb_id)
    if kb is None:
        raise KbError(404, "NOT_FOUND", "知识库不存在", {"kb_id": kb_id})

    keys = kb_repo.list_doc_storage_prefixes(session, kb_id=kb_id)
    for prefix in keys:
        delete_prefix(prefix)  # 幂等：前缀下无对象返回 0，不报错

    doc_count = kb_repo.delete_kb_cascade(session, kb_id=kb_id)
    session.commit()
    return doc_count
