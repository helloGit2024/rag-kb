"""FastAPI 入口。

V-1 骨架版只提供 /healthz，验证容器能起来、后端能响应。
真实路由在后续任务（T08）中添加。
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="RAG-KB", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """存活探针：容器起来就返回 ok。"""
    return {"status": "ok"}
