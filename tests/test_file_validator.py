"""T12 文件校验测试。

分两类：
- 单元（无需外部服务）：类型、大小、md5 计算 —— 用 validate_metadata；
- 集成（需 PostgreSQL）：md5 去重 —— 用 validate_upload（不可达则跳过）。

验收对应 tasks.md T12：重复上传同一文件返回 409 `DUPLICATE_FILE`。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.repos.models import Document
from app.services.file_validator import (
    MAX_FILE_SIZE,
    FileValidationError,
    md5_of_bytes,
    validate_metadata,
    validate_upload,
)


class TestTypeAndSize:
    """单元：类型与大小（纯函数，不依赖数据库）。"""

    def test_markdown_passes(self):
        data = "# 标题\n正文内容。".encode()
        vf = validate_metadata("员工手册.md", data)
        assert vf.source_type == "markdown"
        assert vf.ext == "md"
        assert vf.title == "员工手册.md"
        assert vf.file_size == len(data)
        assert vf.file_md5 == md5_of_bytes(data)

    def test_txt_passes(self):
        vf = validate_metadata("说明.txt", b"hello")
        assert vf.source_type == "txt"
        assert vf.ext == "txt"

    def test_unsupported_type_rejected(self):
        with pytest.raises(FileValidationError) as ei:
            validate_metadata("报表.pdf", b"%PDF-1.4")
        assert ei.value.code == "FILE_TYPE_UNSUPPORTED"
        assert ei.value.status_code == 415

    def test_no_extension_rejected(self):
        with pytest.raises(FileValidationError) as ei:
            validate_metadata("README", b"data")
        assert ei.value.code == "FILE_TYPE_UNSUPPORTED"

    def test_uppercase_extension_accepted(self):
        """扩展名大小写不敏感：.MD 也应通过。"""
        vf = validate_metadata("笔记.MD", "# 标题".encode())
        assert vf.ext == "md"
        assert vf.source_type == "markdown"

    def test_too_large_rejected(self):
        big = b"a" * (MAX_FILE_SIZE + 1)
        with pytest.raises(FileValidationError) as ei:
            validate_metadata("big.md", big)
        assert ei.value.code == "FILE_TOO_LARGE"
        assert ei.value.status_code == 413

    def test_size_boundary_ok(self):
        """恰好等于上限应通过（边界值，不能误杀）。"""
        vf = validate_metadata("edge.md", b"a" * MAX_FILE_SIZE)
        assert vf.file_size == MAX_FILE_SIZE


def _db_available() -> bool:
    """数据库是否可达（不可达则跳过集成测试，而不是判失败）。"""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return False
    try:
        with Session(create_engine(url)) as session:
            session.execute(delete(Document).where(Document.id < 0))
            session.commit()
    except Exception:
        return False
    return True


needs_db = pytest.mark.skipif(
    not _db_available(), reason="PostgreSQL 未启动，跳过去重集成测试"
)

# 专用测试 kb_id，避免污染真实数据
_TEST_KB_ID = 999901


@needs_db
class TestMd5Duplicate:
    """集成：md5 去重（需 PostgreSQL）。"""

    def test_duplicate_returns_duplicate_file(self):
        url = os.environ["DATABASE_URL"]
        data = "T12 去重测试内容（同一文件第二次上传应被拒）".encode()
        md5 = md5_of_bytes(data)
        engine = create_engine(url)
        with Session(engine) as session:
            try:
                session.execute(
                    delete(Document).where(Document.kb_id == _TEST_KB_ID)
                )
                session.commit()

                # 首次：校验通过（库中还没有该 md5）
                vf = validate_upload(
                    "dup.md", data, kb_id=_TEST_KB_ID, session=session
                )
                assert vf.file_md5 == md5

                # 模拟首次已入库：写一行最小 documents 记录
                session.add(
                    Document(
                        kb_id=_TEST_KB_ID,
                        title="dup.md",
                        source_type="markdown",
                        file_md5=md5,
                        file_size=len(data),
                        status="pending",
                    )
                )
                session.commit()

                # 再次上传同一文件：应被拒为 DUPLICATE_FILE / 409
                with pytest.raises(FileValidationError) as ei:
                    validate_upload(
                        "dup.md", data, kb_id=_TEST_KB_ID, session=session
                    )
                assert ei.value.code == "DUPLICATE_FILE"
                assert ei.value.status_code == 409
                assert ei.value.detail["file_md5"] == md5
            finally:
                session.execute(
                    delete(Document).where(Document.kb_id == _TEST_KB_ID)
                )
                session.commit()

    def test_different_content_passes(self):
        """同 kb 内不同内容不受去重影响。"""
        url = os.environ["DATABASE_URL"]
        engine = create_engine(url)
        with Session(engine) as session:
            try:
                session.execute(
                    delete(Document).where(Document.kb_id == _TEST_KB_ID)
                )
                session.commit()

                validate_upload(
                    "a.md", b"first content", kb_id=_TEST_KB_ID, session=session
                )
                # 另一份内容不同（md5 不同）应通过
                vf = validate_upload(
                    "b.md", b"second content", kb_id=_TEST_KB_ID, session=session
                )
                assert vf.file_md5 == md5_of_bytes(b"second content")
            finally:
                session.execute(
                    delete(Document).where(Document.kb_id == _TEST_KB_ID)
                )
                session.commit()
