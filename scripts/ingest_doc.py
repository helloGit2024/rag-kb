"""T13 验收脚本：走完整入库链路（解析 → 分块 → 向量化 → 对象存储 → 落库）。

用法：
    python scripts/ingest_doc.py <md 或 txt 文件路径> [--kb-id 1] [--scope internal]

与 V-1 的 scripts/ingest.py 的区别：
    ingest.py 只做"分块 + 向量化 + 落库"，不碰对象存储（V-1 骨架期简化）；
    本脚本调用正式编排 app/services/ingest.py，会**同时**写对象存储与数据库，
    并独立复核"库中 chunk 行数 == 向量数"（T13 验收标准）。

★ 本脚本仅供验收/自测；正式链路由 worker 调用 ingest
  （R10：离线任务不得在 HTTP 请求里同步跑）。

输出约定（诊断走 stderr，与项目其他脚本一致）：
    分块数 / 向量数 / 库中行数 / 吞吐 / 对象 key / 清理命令 → stderr；
    doc_id → stdout（唯一一行，便于下游脚本引用）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 免 PYTHONPATH：把项目根加入 sys.path，脚本即可直接运行（与 parse_md.py 同约定）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.repos.models import Chunk  # noqa: E402
from app.services.file_validator import FileValidationError  # noqa: E402
from app.services.ingest import IngestError, ingest_document  # noqa: E402


def load_env() -> dict[str, str]:
    """读项目根 .env（KEY=VALUE），忽略注释与空行。

    把 compose 服务名换成 localhost，宿主与容器共用同一份 .env 不漂移：
      DATABASE_URL 的 @postgres: → @localhost:（与 scripts/ingest.py 同约定）
      MINIO_ENDPOINT 的 ://silo:  → ://localhost:（与 scripts/store_file.py 同约定）
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
    if "@postgres:" in env.get("DATABASE_URL", ""):
        env["DATABASE_URL"] = env["DATABASE_URL"].replace("@postgres:", "@localhost:")
    if "://silo:" in env.get("MINIO_ENDPOINT", ""):
        env["MINIO_ENDPOINT"] = env["MINIO_ENDPOINT"].replace("://silo:", "://localhost:")
    return env


def _print_cleanup_hint(doc_id: int, doc_uuid: str) -> None:
    """打印本次数据的清理命令，避免自测/验收在库里和对象存储里留垃圾。"""
    user = os.getenv("POSTGRES_USER", "ragkb")
    db = os.getenv("POSTGRES_DB", "ragkb")
    print("── 清理本次数据（可选）──", file=sys.stderr)
    print(
        f"  docker compose -f docker-compose.dev.yml exec -T postgres psql -U {user} "
        f'-d {db} -c "DELETE FROM chunks WHERE doc_id={doc_id}; '
        f'DELETE FROM documents WHERE id={doc_id};"',
        file=sys.stderr,
    )
    print(
        f"  python -c \"import sys; sys.path.insert(0,'.'); "
        f"from app.core.storage import delete_prefix; "
        f"print(delete_prefix('docs/{doc_uuid}/'))\"",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="T13 验收：完整入库链路")
    parser.add_argument("file_path", help="待入库的 md / txt 文件路径")
    parser.add_argument("--kb-id", type=int, default=1, help="知识库 ID")
    parser.add_argument(
        "--scope", default="internal", help="可见范围（V-1 固定 internal）"
    )
    args = parser.parse_args()

    path = Path(args.file_path)
    if not path.is_file():
        sys.exit(f"文件不存在：{path}")

    env = load_env()
    if not env.get("DATABASE_URL"):
        sys.exit("DATABASE_URL 未设置：请在项目根 .env 中配置（cp .env.example .env）")
    os.environ.update(env)

    data = path.read_bytes()

    t0 = time.perf_counter()
    try:
        engine = create_engine(env["DATABASE_URL"])
        with Session(engine) as session:
            result = ingest_document(
                session,
                kb_id=args.kb_id,
                filename=path.name,
                data=data,
                scope=args.scope,
            )
    except FileValidationError as e:
        sys.exit(f"校验失败（{e.code}）：{e.message}")
    except IngestError as e:
        sys.exit(f"入库失败：{e}")
    elapsed = time.perf_counter() - t0

    # 独立复核：库中实际写入的 chunk 行数必须等于向量数（T13 验收标准）
    with Session(engine) as session:
        db_count = session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.doc_id == result.doc_id)
        )
    db_count = int(db_count or 0)

    throughput = result.chunk_count / elapsed if elapsed > 0 else 0.0
    print(f"doc_id={result.doc_id}，doc_uuid={result.doc_uuid}", file=sys.stderr)
    print(f"分块数={result.chunk_count}，向量数={result.vector_count}", file=sys.stderr)
    print(
        f"库中 chunk 行数={db_count}，耗时 {elapsed:.2f}s，"
        f"吞吐 {throughput:.1f} 块/秒",
        file=sys.stderr,
    )
    print(
        "（验收线：吞吐 ≥8 块/秒，spec.md 3.4；低于此线说明需切 TEI）",
        file=sys.stderr,
    )
    print(f"原文对象={result.raw_key}", file=sys.stderr)
    print(f"解析对象={result.parsed_key}", file=sys.stderr)

    if db_count != result.vector_count:
        sys.exit(
            f"❌ 验收失败：库中 chunk 行数 {db_count} != 向量数 {result.vector_count}"
        )
    print("✅ 验收通过：chunk 数与向量数一致", file=sys.stderr)

    _print_cleanup_hint(result.doc_id, result.doc_uuid)

    print(result.doc_id)


if __name__ == "__main__":
    main()
