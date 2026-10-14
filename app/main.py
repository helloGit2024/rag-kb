"""FastAPI 入口。

V-1 骨架版路由：/healthz（存活探针）+ POST /api/v1/query（问答）。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1.query import ApiError
from app.api.v1.query import router as query_router

app = FastAPI(title="RAG-KB", version="0.1.0")
app.include_router(query_router)


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


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体校验失败 → 400 + QUESTION_INVALID。

    契约（openapi.yaml / AC-11）要求 400+QUESTION_INVALID，而 FastAPI 默认返回 422，
    因此这里改写。校验在进入路由体之前完成，所以"超长问题不产生模型调用"由这一点保证。
    V-1 只有 /query 一个带请求体的接口，故统一映射为 QUESTION_INVALID。
    """
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "QUESTION_INVALID",
                "message": "问题参数不合法（须为 1—2000 字的非空文本）",
                "detail": {},
            }
        },
    )


@app.exception_handler(Exception)
async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底 → 500 + INTERNAL_ERROR（R7：显式失败，返回结构化错误，不吐裸栈）。"""
    print(f"⚠️ 未处理异常：{exc!r}")
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
