"""任务状态接口的响应模型（V0.1 · T23）。

字段逐条对齐 docs/openapi.yaml components.schemas.ParseTask：
task_id / doc_id / task_type / status / progress / retry_count / error_reason。
★ 刻意不返回 payload：里面有 staging_key / file_md5 等内部信息，
  契约里没有这个字段，泄漏出去没有任何收益。
"""

from __future__ import annotations

from pydantic import BaseModel


class ParseTaskOut(BaseModel):
    """GET /tasks/{task_id} 响应体（openapi components.schemas.ParseTask）。"""

    task_id: int
    doc_id: int | None
    task_type: str
    status: str
    progress: int
    retry_count: int
    error_reason: str | None
