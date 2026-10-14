"""T09 冒烟测试：ingest → ask 全链路（真实 PG + Ollama + 生成模型）。

★ 依赖：PostgreSQL（pgvector）+ Ollama（bge-m3 + 生成模型）必须在运行。
  这是端到端冒烟测试，用独有 fixture 文档验证三件事：
  ① ingest 后库里有 chunk；
  ② ask.py 提已知问题返回答案且带引用；
  ③ 提库里没有的问题恰好输出固定拒答句（R5）。

fixture 文档含独有数字 237（验证 R6 原文照抄），session 结束后删除，
可重复跑、不污染开发库。
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.core.embedding import embed_texts
from app.repos.chunk_repo import search_chunks
from app.repos.models import Chunk, Document
from app.services.generator import REFUSAL_TEXT

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 独有内容：开发库中不可能存在；数字 237 用于验证 R6 原文照抄
FIXTURE_TITLE = "smoke_fixture_萤火计划出差补贴细则.txt"
FIXTURE_TEXT = """萤火计划出差补贴细则

为保障萤火计划野外观测任务顺利进行，特制定本补贴细则。

第一条 补贴标准。前往青海冷湖基地出差的员工，每日补贴 237 元，按自然日计算。

第二条 发放方式。补贴随次月工资一并发放，无需另行报销。
"""


def _services_available() -> bool:
    """检查 PG 和嵌入服务是否可达（不可达则跳过测试）。"""
    try:
        embed_texts(["ping"])
    except Exception:
        return False
    try:
        search_chunks([0.0] * 1024, ["internal"], top_k=1)
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="PostgreSQL 或 Ollama 未启动，跳过冒烟测试",
)


def _run_script(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    """以子进程跑 scripts/ 下的脚本（贴近真实命令行使用方式）。"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / script), *args],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        env=env,
        timeout=300,
    )


@pytest.fixture(scope="session")
def ingested_fixture_doc(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[int, None, None]:
    """入库独有 fixture 文档，返回 documents.id；session 结束后删除它及其 chunks。"""
    engine = create_engine(os.environ["DATABASE_URL"])

    # 先清掉上次异常中断可能留下的同名文档，避免 md5 去重拒绝本次入库
    with Session(engine) as session:
        stale_ids = session.scalars(
            select(Document.id).where(Document.title == FIXTURE_TITLE)
        ).all()
        if stale_ids:
            session.execute(delete(Chunk).where(Chunk.doc_id.in_(stale_ids)))
            session.execute(delete(Document).where(Document.id.in_(stale_ids)))
            session.commit()

    txt = tmp_path_factory.mktemp("smoke") / FIXTURE_TITLE
    txt.write_text(FIXTURE_TEXT, encoding="utf-8")
    proc = _run_script("ingest.py", str(txt))
    assert proc.returncode == 0, f"ingest 失败：{proc.stderr or proc.stdout}"

    with Session(engine) as session:
        doc_id = session.scalars(
            select(Document.id).where(Document.title == FIXTURE_TITLE)
        ).one()

    yield doc_id

    with Session(engine) as session:
        session.execute(delete(Chunk).where(Chunk.doc_id == doc_id))
        session.execute(delete(Document).where(Document.id == doc_id))
        session.commit()


class TestSmoke:
    """T09 验收的三件事（docs/tasks.md V-1 表 T09 行）。"""

    def test_ingest_writes_chunks(self, ingested_fixture_doc: int) -> None:
        """① ingest 一份 txt 后库里有 chunk。"""
        engine = create_engine(os.environ["DATABASE_URL"])
        with Session(engine) as session:
            count = session.scalars(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.doc_id == ingested_fixture_doc)
            ).one()
        assert count > 0, "ingest 后 chunks 表应有该文档的块"

    def test_ask_known_question_returns_answer_with_citations(
        self, ingested_fixture_doc: int
    ) -> None:
        """② 提已知问题：退出码 0、非拒答、带引用、数字 237 原文照抄（R6）。"""
        proc = _run_script("ask.py", "前往青海冷湖基地出差每日补贴多少元？")

        assert proc.returncode == 0, f"ask 失败：{proc.stderr}"
        assert REFUSAL_TEXT not in proc.stdout, f"已知问题不应拒答：{proc.stdout}"
        assert "引用：" in proc.stdout, f"答案应带引用区块：{proc.stdout}"
        assert "237" in proc.stdout, f"答案应原文照抄数字 237（R6）：{proc.stdout}"

    def test_ask_unknown_question_exact_refusal(self) -> None:
        """③ 提库里没有的问题：stdout 恰好是固定拒答句（R5）。"""
        proc = _run_script("ask.py", "公司今年的年会在哪里举办？")

        assert proc.returncode == 0, f"ask 失败：{proc.stderr}"
        assert proc.stdout.strip() == REFUSAL_TEXT, (
            f"未知问题应恰好输出固定拒答句，实际 stdout：{proc.stdout!r}"
        )
