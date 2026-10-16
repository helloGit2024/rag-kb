"""FastAPI 入口。

路由：/healthz（存活探针）+ POST /api/v1/query（问答，V-1）
      + /api/v1/documents（上传 V0.1 · T18；列表 / 删除 · T22）
      + /api/v1/knowledge-bases（知识库 CRUD，V0.1 · T21）。
"""

from __future__ import annotations

import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1.documents import router as documents_router
from app.api.v1.knowledge_bases import router as knowledge_bases_router
from app.api.v1.query import ApiError
from app.api.v1.query import router as query_router
from app.services.document import DocumentError
from app.services.file_validator import FileValidationError
from app.services.kb import KbError

app = FastAPI(title="RAG-KB", version="0.1.0")
app.include_router(query_router)
app.include_router(documents_router)
app.include_router(knowledge_bases_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """存活探针：容器起来就返回 ok。"""
    return {"status": "ok"}


@app.exception_handler(ApiError)
async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """业务异常 → 统一错误信封 {"error": {code, message, detail}}。

    信封格式见 api-data 规则第一节。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"code": exc.code, "message": exc.message, "detail": exc.detail}
        },
    )


@app.exception_handler(FileValidationError)
@app.exception_handler(KbError)
@app.exception_handler(DocumentError)
async def _service_error_handler(
    request: Request, exc: FileValidationError | KbError | DocumentError
) -> JSONResponse:
    """服务层异常（T12 文件校验 409/413/415、T21 知识库 400/404/409、
    T22 文档 400/404）→ 统一错误信封。

    FileValidationError / KbError / DocumentError 字段结构一致
    （status_code/code/message/detail），因此共用同一个处理器（同一个函数挂
    三个装饰器），避免抄第三份一模一样的响应构造代码。分开于 ApiError 注册
    是因为它们来自 service 层：service 层不应该 import api 层的异常类。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"code": exc.code, "message": exc.message, "detail": exc.detail}
        },
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体校验失败 → 400。

    契约（openapi.yaml / AC-11）要求 /query 返回 400+QUESTION_INVALID，
    而 FastAPI 默认返回 422，因此这里改写。
    ★ 按路径分流：T21 起不再是"只有 /query 带请求体"——
      /knowledge-bases 的 name 超长、缺字段等也走这里。若统一套用
      QUESTION_INVALID 的"问题参数不合法"文案，用户会在建知识库时
      收到一句关于"问题"的报错，排障时属实误导。
      ★ 代码仍是 QUESTION_INVALID：契约 400 级只有这一个码可用（不得自创，
        见 api-contract.md 错误码清单）；已记入 OPEN-QUESTIONS，待契约
        补通用入参码后统一替换。
    """
    is_query = request.url.path.startswith("/api/v1/query")
    if is_query:
        message = "问题参数不合法（须为 1—2000 字的非空文本）"
    else:
        message = "请求参数不合法（字段缺失或超长）"
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "QUESTION_INVALID",
                "message": message,
                "detail": {},
            }
        },
    )


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底 → 500 + INTERNAL_ERROR（R7：显式失败，返回结构化错误，不吐裸栈）。"""
    print(f"⚠️ 未处理异常：{exc!r}", file=sys.stderr)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
                "detail": {},
            }
        },
    )
