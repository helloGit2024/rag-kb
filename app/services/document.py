"""文档列表与删除的业务编排（V0.1 · T22）。

职责：入参校验 → 调 repo → 控事务 → 删除时协调对象存储。
★ 不写 SQL（在 app/repos/document_repo.py）；不产 HTTP 响应（在 app/api/）。

上游：上传与入库链路的产物（documents/chunks）。
下游：GET /api/v1/documents、DELETE /api/v1/documents/{doc_id}。

T22 定档的四个决策（2026-10-16 与项目所有者确认）：
1. **列表按权限过滤**：可见性条件复用 T17b 的两轴口径（scope + kb），
   写在 SQL WHERE 里（R2）。理由：文档标题常含敏感信息（如"XX 部门降薪方案"），
   列全部等于信息泄露。
2. **`uploaded_by` 暂返回 null**：documents 行由 worker 异步创建，上传接口
   并不知道将来是谁建的（`perm.user_id` 从未进过 worker）。本任务不碰 T13/T18
   已验收的入库链路，缺口记入 OPEN-QUESTIONS 另排任务。
3. **删除走级联三步**：真删 chunks → documents 软删 → 删对象存储；
   缓存失效与审计日志留 V1（与 T21 知识库删除同口径）。
4. **status 参数严格按 openapi 六态校验**，不顺从实现的真实分布。

★ 关于实现里的 status 只有 `done` 一个值：`insert_document` 只在整条入库链路
  成功时被调用（失败在解析阶段就中断、根本不建 documents 行），因此列表里
  实际只会出现 `done`。这不是 bug，是 T13 的"失败不落半成品"设计；六态里的
  其余五态是为 V1 的分阶段状态机预留的。前端选它们会得到空列表。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from app.core.storage import delete_prefix
from app.repos import document_repo
from app.repos.document_repo import DOCUMENT_STATUSES, DocumentRow


class DocumentError(Exception):
    """文档业务异常（带结构化错误码）。

    字段与 app/services/kb.py 的 KbError / file_validator 的 FileValidationError
    对齐——服务层不 import api 层的 ApiError，由 main.py 的处理器统一转错误信封。
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


def _validate_status(status: str | None) -> str | None:
    """校验 status 过滤值（openapi Document.status 六态）。

    空白串视同"不传"（前端 drop-down 常带空选项）。非法值显式失败——
    与 T21 的知识库校验同一取向：宁可 400，不静默返回一个空列表让用户
    以为是"没有数据"。
    """
    if status is None:
        return None
    value = status.strip()
    if not value:
        return None
    if value not in DOCUMENT_STATUSES:
        raise DocumentError(
            400,
            "QUESTION_INVALID",
            f"status 取值非法：{status!r}（仅允许 {' / '.join(DOCUMENT_STATUSES)}）",
            {"allowed": list(DOCUMENT_STATUSES)},
        )
    return value


def list_documents(
    session: Session,
    *,
    scope_keys: Sequence[str],
    kb_ids: Sequence[int] | None = None,
    kb_id: int | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[DocumentRow], int]:
    """分页列出当前用户可见的文档，返回 (rows, total)。

    Raises:
        DocumentError: 400（status 非法）。
        ValueError: scope_keys 为空（R2/R7：宁可显式失败，也不列出全部）。
    """
    if not scope_keys:
        # R7：与 chunk_repo.search_chunks 同一道防线——没解析出可见范围
        # 就绝不能"默默列出全部"，那是最不该发生的越权形态。
        raise ValueError("scope_keys 不能为空，否则会列出越权文档（R2/R7）")

    validated_status = _validate_status(status)
    return document_repo.list_documents(
        session,
        scope_keys=scope_keys,
        kb_ids=kb_ids,
        kb_id=kb_id,
        status=validated_status,
        page=page,
        page_size=page_size,
    )


def delete_document(
    session: Session,
    *,
    doc_id: int,
    scope_keys: Sequence[str],
    kb_ids: Sequence[int] | None = None,
) -> int:
    """删除文档（级联三步），返回真删掉的 chunks 行数。

    顺序（与 T21 的 delete_kb 同口径，理由同）：
      ① 先按 `docs/{doc_uuid}/` 前缀删对象（幂等，前缀下无对象返回 0）；
      ② 真删 chunks；
      ③ documents 软删（is_active=false）；
      ④ 一次 commit。

    ★ 为什么"先对象、后库行"：反过来一旦对象删除失败，库行已软删、
      列表里看不见了，那些对象就变成**永久孤儿**——静默的数据留存，
      合规上比删不掉更糟。先删对象时若失败，事务未提交、库行仍有效，
      重试即可；对象删除本身幂等，不会因为重试而误删别的东西。

    ★ 可见范围（T22 定档）：不在 `scope_keys` / `kb_ids` 范围内的文档按
      "不存在"处理（404），**不返回 403**——403 等于告诉调用方"这个 id 确实存在"。
      过滤条件与列表一致、同样在 SQL WHERE 里（R2）。

    Raises:
        DocumentError: 404 NOT_FOUND（不存在、已软删、或不在可见范围内）。
        ValueError: scope_keys 为空（R2/R7）。
    """
    if not scope_keys:
        raise ValueError("scope_keys 不能为空，否则会删到越权文档（R2/R7）")

    doc = document_repo.get_active_document(
        session, doc_id=doc_id, scope_keys=scope_keys, kb_ids=kb_ids
    )
    if doc is None:
        raise DocumentError(
            404, "NOT_FOUND", f"文档不存在：{doc_id}", {"doc_id": doc_id}
        )

    prefixes = document_repo.list_doc_storage_prefixes(session, doc_id=doc_id)
    for prefix in prefixes:
        delete_prefix(prefix)

    deleted_chunks = document_repo.delete_chunks_by_doc(session, doc_id=doc_id)
    document_repo.soft_delete_document(session, doc_id=doc_id)
    session.commit()
    return deleted_chunks
