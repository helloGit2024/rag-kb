"""文件上传前置校验（V0.1 · T12）。

职责：文档在进入解析 / 向量化之前，必须先过三道校验：
1. 类型：只允许 .md / .txt（契约 `FILE_TYPE_UNSUPPORTED` / 415）
2. 大小：≤ 20 MB（契约 `FILE_TOO_LARGE` / 413）
3. 去重：同 kb 内 md5 相同则拒绝（契约 `DUPLICATE_FILE` / 409）

错误码全部取自 `docs/api-contract.md` 的清单，**不得自创**。
校验顺序：类型 → 大小 → 去重（先便宜后昂贵：扩展名最便宜，查库最贵）。

★ 不做空文件（0 字节）校验：契约错误码清单里没有"空内容"对应的代码，
  空内容属数据质量问题，交由 T13 入库编排处理（已与项目所有者确认）。

★ 20 MB 上限与扩展名白名单暂以模块常量维护；若要挪进 `config/`（受控配置），
  需单独提任务（R12 / R9）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.repos.document_repo import find_active_by_md5

# 上传限制（与 docs/openapi.yaml 的 POST /api/v1/documents 一致）
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB
# 扩展名 → source_type（取值见 docs/data-model.md 2.3，V0.1 只支持这两种）
ALLOWED_EXTENSIONS: dict[str, str] = {"md": "markdown", "txt": "txt"}


class FileValidationError(Exception):
    """带结构化错误码的校验失败（R7：显式失败，由上层映射成错误信封）。

    字段与 app/api/v1/query.py 的 ApiError 对齐，便于 T22 直接映射为 HTTP 响应。
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
class ValidatedFile:
    """通过校验的文件信息，交给 T13 入库编排使用。"""

    title: str
    source_type: str  # markdown / txt
    ext: str
    file_md5: str
    file_size: int


def md5_of_bytes(data: bytes) -> str:
    """文件内容的 md5（十六进制）。"""
    return hashlib.md5(data).hexdigest()


def validate_metadata(filename: str, data: bytes) -> ValidatedFile:
    """类型 + 大小校验（纯函数，不查库，单元可测）。

    Args:
        filename: 上传时的文件名（由扩展名判类型）。
        data: 文件字节内容。

    Raises:
        FileValidationError: 类型不支持（415）/ 超过大小上限（413）。
    """
    ext = Path(filename).suffix.lstrip(".").lower()
    source_type = ALLOWED_EXTENSIONS.get(ext)
    if source_type is None:
        raise FileValidationError(
            415,
            "FILE_TYPE_UNSUPPORTED",
            f"不支持的文件类型：.{ext or '(无扩展名)'}（V0.1 仅支持 .md / .txt）",
            {"filename": filename, "allowed": sorted(ALLOWED_EXTENSIONS)},
        )

    file_size = len(data)
    if file_size > MAX_FILE_SIZE:
        raise FileValidationError(
            413,
            "FILE_TOO_LARGE",
            "文件超过 20 MB 上限",
            {"file_size": file_size, "max_size": MAX_FILE_SIZE},
        )

    return ValidatedFile(
        title=Path(filename).name,
        source_type=source_type,
        ext=ext,
        file_md5=md5_of_bytes(data),
        file_size=file_size,
    )


def ensure_not_duplicate(
    session: Session, *, kb_id: int, file_md5: str
) -> None:
    """md5 去重（查库）。

    Raises:
        FileValidationError: 同 kb 内已存在相同 md5 的文档（409）。
    """
    dup_id = find_active_by_md5(session, kb_id=kb_id, file_md5=file_md5)
    if dup_id is not None:
        raise FileValidationError(
            409,
            "DUPLICATE_FILE",
            "该文件已存在（同知识库内 md5 重复）",
            {"doc_id": dup_id, "file_md5": file_md5},
        )


def validate_upload(
    filename: str, data: bytes, *, kb_id: int, session: Session
) -> ValidatedFile:
    """完整上传校验：类型 → 大小 → 去重（T22 上传接口调用此函数）。

    Raises:
        FileValidationError: 见 validate_metadata / ensure_not_duplicate。
    """
    validated = validate_metadata(filename, data)
    ensure_not_duplicate(session, kb_id=kb_id, file_md5=validated.file_md5)
    return validated
