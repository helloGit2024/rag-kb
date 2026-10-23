"""问答接口的数据结构（字段严格对齐 docs/openapi.yaml，不自行发明）。

契约来源：
  - POST /api/v1/query 的 requestBody（question / top_k）
  - components.schemas.QueryResult（required: answer, refused,
    degraded, citations, latency_ms）
  - components.schemas.Citation（required: index, doc_id, chunk_id, snippet）

V-1 只声明并实现 question 与 top_k；kb_ids / filters / channel 属 V2，
在 V-1 既不声明也不静默忽略——避免"声明了却无效"的假接口。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# openapi.yaml 中 question 的约束：minLength 1 / maxLength 2000。
# 校验在进入路由体之前完成，因此"超长问题不调用模型"由这一点天然保证（AC-11）。
_MAX_QUESTION_LEN = 2000


class QueryRequest(BaseModel):
    """问答请求体。"""

    question: str = Field(..., min_length=1, max_length=_MAX_QUESTION_LEN)
    top_k: int = Field(5, ge=1, le=20)

    @field_validator("question")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """纯空白（如 "   "）不满足 min_length，但语义上同样是空问题——显式拒绝。"""
        stripped = value.strip()
        if not stripped:
            raise ValueError("question 不能为空白")
        return stripped


class CitationOut(BaseModel):
    """一条引用。字段与 openapi.yaml 的 Citation 一一对应。"""

    index: int
    doc_id: int
    doc_title: str
    chunk_id: int
    char_start: int
    char_end: int
    snippet: str
    score: float


class QueryResult(BaseModel):
    """问答响应。

    三种非成功但非错误的状态按 api-data 规则表达：
      - 拒答：refused=true，HTTP 仍为 200；
      - 降级：degraded=true，HTTP 仍为 200；
      - 错误：不走本模型，走统一的错误信封 {"error": {...}}。

    T32 延迟与成本埋点字段全部声明：
      - latency_ms（required）= retrieve_ms + generate_ms
      - rerank_ms（nullable）：V0.1 故意不做重排（项目规则 ❌ 项）→ 始终 null
      - cost_cny（nullable）：本地 Ollama 时 null，云端模型上线后自动有值
      - trace_id（nullable）：V0.1 无链路追踪，留 null；V1 加真实 trace_id
      - query_log_id（nullable）：T34 起为本次问答在 query_logs 的行 id；
        落表失败或未带身份时为 null
    """

    # 契约字段名 model_used 恰好落在 pydantic 的 "model_" 保护前缀里，
    # 关闭该前缀检查以免刷告警——字段名以 openapi.yaml 为准，不能改。
    model_config = ConfigDict(protected_namespaces=())

    answer: str
    refused: bool
    degraded: bool
    citations: list[CitationOut]
    latency_ms: int
    retrieve_ms: int | None = None
    rerank_ms: int | None = None  # V0.1 故意不做重排 → 始终 null
    generate_ms: int | None = None
    cost_cny: float | None = None  # 本地 Ollama 免费 → null
    model_used: str | None = None
    trace_id: str | None = None  # V0.1 无链路追踪 → null
    query_log_id: int | None = None  # T34：落表成功时为行 id，否则 null
