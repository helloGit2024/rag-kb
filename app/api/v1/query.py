"""POST /api/v1/query 路由（V-1）。

本层只做三件事：接收已校验的请求 → 调 service → 把结果/异常映射成契约响应。
不写业务逻辑、不查库（目录约定）。
"""

from __future__ import annotations

import sys
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.core.embedding import EmbeddingError
from app.core.llm_gateway import GatewayError
from app.repos.db import get_session
from app.schemas.query import CitationOut, QueryRequest, QueryResult
from app.services import qa
from app.services.permission import (
    PermissionBundle,
    ScopeError,
    resolve_permission,
)


class ApiError(Exception):
    """带结构化错误码的业务异常，由 main.py 的处理器转成统一错误信封。"""

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


router = APIRouter(prefix="/api/v1", tags=["query"])


def _require_user_permission(
    session: Annotated[Session, Depends(get_session)],
    x_kb_user: Annotated[str | None, Header(alias="X-KB-User")] = None,
) -> PermissionBundle:
    """从 `X-KB-User` header 解析出当前用户的完整权限包（scope_keys + kb_ids，T17b）。

    V0.1 的临时身份方案（api-contract.md 3.2）：直接传用户名，V1 换 JWT。
    ★ security.md 2.2：任何接口进入业务逻辑前必须完成"身份 → 可见范围"解析；
      scope_keys 交给 scope_key 维度、kb_ids 交给 kb_id 维度，两轴独立
      （data-model 第三节正确写法：两条件 SQL，不是一个）。

    Raises:
        ApiError 401 UNAUTHENTICATED: 未提供 header / 用户不存在 / 已禁用。
            **不兜底成某个默认账号**——默认账号 = 隐式越权入口。
    """
    username = (x_kb_user or "").strip()
    if not username:
        raise ApiError(401, "UNAUTHENTICATED", "未提供身份（缺少 X-KB-User）")
    try:
        return resolve_permission(session, username=username)
    except ScopeError as exc:
        # R7：身份无法解析时显式失败；响应体不回显内部细节
        raise ApiError(401, "UNAUTHENTICATED", "身份无效或已禁用") from exc


@router.post("/query", response_model=QueryResult)
def query(
    req: QueryRequest,
    perm: Annotated[PermissionBundle, Depends(_require_user_permission)],
) -> QueryResult:
    """知识库问答。

    拒答与正常回答同为 200（拒答是正常业务结果，不是错误，见 api-data 规则第二节）。

    ★ 权限：`X-KB-User` header 经 `_require_user_permission` 解析成完整权限包
      （scope_keys 非 kb 维度 + kb_ids kb 维度），两轴分别传给检索 SQL 做过滤
      （R2：过滤在 SQL WHERE 内完成，不在应用层筛）。
    """
    try:
        result = qa.answer_question(
            req.question,
            top_k=req.top_k,
            scope_keys=perm.scope_keys,
            kb_ids=perm.kb_ids,
        )
    except GatewayError as exc:
        # 生成模型不可用：503 + MODEL_UNAVAILABLE（R7 显式失败）。
        # detail 只回传可判定的信息，不回传内网地址/密钥（R1）。
        print(
            f"⚠️ 生成模型调用失败：kind={exc.kind} attempts={exc.attempts}",
            file=sys.stderr,
        )
        raise ApiError(
            503,
            "MODEL_UNAVAILABLE",
            "生成模型不可用，请稍后重试",
            {"kind": exc.kind, "attempts": exc.attempts},
        ) from exc
    except EmbeddingError as exc:
        print(f"⚠️ 嵌入模型调用失败：{exc!r}", file=sys.stderr)
        raise ApiError(
            503,
            "MODEL_UNAVAILABLE",
            "嵌入模型不可用，请稍后重试",
            {"reason": "embedding_unavailable"},
        ) from exc

    return QueryResult(
        answer=result.answer,
        refused=result.refused,
        # V-1 的网关尚不区分"是否走了备用模型"，且 .env 未配置 LLM_FALLBACK_MODEL，
        # 实际必为 False（未发生降级）。真实降级上报随网关在 V0.1 T32 补齐。
        degraded=False,
        citations=[
            CitationOut(
                index=c.index,
                doc_id=c.doc_id,
                doc_title=c.doc_title,
                chunk_id=c.chunk_id,
                char_start=c.char_start,
                char_end=c.char_end,
                snippet=c.snippet,
                score=c.score,
            )
            for c in result.citations
        ],
        latency_ms=result.latency_ms,
        retrieve_ms=result.retrieve_ms,
        generate_ms=result.generate_ms,
        model_used=result.model_used,
        cost_cny=result.cost_cny,
    )
