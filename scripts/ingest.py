"""V-1 入库脚本：读 txt → 递归字符分块 → Ollama 向量化 → 写 pgvector。

用法：
    python scripts/ingest.py <txt 文件路径> [--kb-id 1] [--scope internal]

★ V-1 说明：
- 本脚本在宿主机运行：连 localhost 的 PostgreSQL（compose 已映射 5432）
  与本机 Ollama（11434）。.env 里 DATABASE_URL 的 compose 服务名 postgres
  会被自动替换为 localhost，避免维护两份配置。
- 正式入库链路在 T13 搬进 app/services/ + worker（R10 在线/离线隔离），
  本脚本仅用于 V-1 验收与冒烟测试。
- scope_key V-1 固定直传 scope 值，不做真实权限推导（T15 / V0.1 启用）。
- ★ 企业级真实入库需将嵌入服务从 Ollama 切换为 TEI（见 app/core/embedding.py）。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.embedding import check_normalized, embed_texts
from app.repos.models import Chunk, Document
from app.services.chunker import chunk_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    """读项目根 .env（KEY=VALUE），忽略注释与空行。

    宿主运行时把 DATABASE_URL 里的 compose 服务名 postgres 替换为 localhost，
    容器内外共用同一份 .env 而不漂移。
    """
    env: dict[str, str] = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    if "postgres:" in env.get("DATABASE_URL", ""):
        env["DATABASE_URL"] = env["DATABASE_URL"].replace("@postgres:", "@localhost:")
    return env


def md5_of(path: Path) -> str:
    """流式计算文件 md5（大文件不全量读内存）。"""
    h = hashlib.md5()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="V-1 文档入库脚本")
    parser.add_argument("txt_path", help="待入库的 txt 文件路径")
    parser.add_argument("--kb-id", type=int, default=1, help="知识库 ID（V-1 固定 1）")
    parser.add_argument(
        "--scope", default="internal", help="可见范围（V-1 固定 internal）"
    )
    args = parser.parse_args()

    path = Path(args.txt_path)
    if not path.is_file():
        sys.exit(f"文件不存在：{path}")

    env = load_env()
    database_url = env.get("DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        sys.exit("DATABASE_URL 未设置：请在项目根 .env 中配置")

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        sys.exit("文件内容为空")

    file_md5 = md5_of(path)

    engine = create_engine(database_url)
    with Session(engine) as session:
        # md5 去重（数据模型 2.3：同 kb 内 md5 相同则拒绝重复入库）
        dup = session.execute(
            select(Document.id).where(
                Document.kb_id == args.kb_id,
                Document.file_md5 == file_md5,
                Document.is_active.is_(True),
            )
        ).first()
        if dup:
            sys.exit(
                f"该文件已入库（documents.id={dup[0]}，md5={file_md5}），拒绝重复入库"
            )

        # 1) 分块（512 字 + 64 重叠，策略见 app/services/chunker.py）
        t0 = time.perf_counter()
        chunks = chunk_text(text, chunk_size=512, chunk_overlap=64)
        t_chunk = time.perf_counter() - t0
        if not chunks:
            sys.exit("分块结果为空")
        print(f"分块完成：{len(chunks)} 块，耗时 {t_chunk:.2f}s")

        # 2) 向量化（吞吐验收线 ≥ 8 块/秒，spec.md 3.4）
        t0 = time.perf_counter()
        vectors = embed_texts([c.content for c in chunks])
        embed_seconds = time.perf_counter() - t0
        throughput = len(chunks) / embed_seconds if embed_seconds > 0 else 0.0
        avg_norm = check_normalized(vectors[:8])
        print(
            f"向量化完成：共 {len(chunks)} 块，耗时 {embed_seconds:.2f} 秒，"
            f"吞吐 {throughput:.1f} 块/秒（验收线 ≥8），平均 norm={avg_norm:.4f}"
        )
        if throughput < 8:
            print("⚠️ 吞吐未达验收线 8 块/秒，V0.1 前需切换 TEI 或调 batch_size")

        # 3) 落库（单事务：documents + chunks，异常整体回滚）
        doc = Document(
            kb_id=args.kb_id,
            title=path.name,
            source_type="txt",
            file_md5=file_md5,
            file_size=path.stat().st_size,
            status="done",
            scope=args.scope,
            version=1,
        )
        session.add(doc)
        session.flush()  # 拿到 doc.id 再插 chunks

        session.add_all(
            Chunk(
                doc_id=doc.id,
                kb_id=args.kb_id,
                chunk_index=c.chunk_index,
                content=c.content,
                char_start=c.char_start,
                char_end=c.char_end,
                embedding=vectors[c.chunk_index],
                # V-1：scope_key 由 doc.scope 推导，无推导逻辑前固定直传
                scope_key=args.scope,
            )
            for c in chunks
        )
        session.commit()
        print(f"入库完成：documents.id={doc.id}，写入 {len(chunks)} 条 chunks")


if __name__ == "__main__":
    main()
