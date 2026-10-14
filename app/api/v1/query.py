"""POST /api/v1/query 路由（V-1）。

本层只做三件事：接收已校验的请求 → 调 service → 把结果/异常映射成契约响应。
不写业务逻辑、不查库（目录约定）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.core.embedding import EmbeddingError
from app.core.llm_gateway import GatewayError
from app.schemas.query import CitationOut, QueryRequest, QueryResult
from app.services import qa


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

# V-1 没有用户体系：固定传 ["internal"]（与种子数据、test_retrieval 一致），
# 不做真实权限隔离。
# ★ V0.1 T15 起改为从鉴权依赖（Depends）解析出的真实 user_scope（R2）——
#   service/repo 的签名已就绪，届时只改这一处的取值来源。
_V1_SCOPE_KEYS = ["internal"]


@router.post("/query", response_model=QueryResult)
def query(req: QueryRequest) -> QueryResult:
    """知识库问答。

    拒答与正常回答同为 200（拒答是正常业务结果，不是错误，见 api-data 规则第二节）。
    """
    try:
        result = qa.answer_question(
            req.question, top_k=req.top_k, user_scope_keys=_V1_SCOPE_KEYS
        )
    except GatewayError as exc:
        # 生成模型不可用：503 + MODEL_UNAVAILABLE（R7 显式失败）。
        # detail 只回传可判定的信息，不回传内网地址/密钥（R1）。
        print(f"⚠️ 生成模型调用失败：kind={exc.kind} attempts={exc.attempts}")
        raise ApiError(
            503,
            "MODEL_UNAVAILABLE",
            "生成模型不可用，请稍后重试",
            {"kind": exc.kind, "attempts": exc.attempts},
        ) from exc
    except EmbeddingError as exc:
        print(f"⚠️ 嵌入模型调用失败：{exc!r}")
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
    )
