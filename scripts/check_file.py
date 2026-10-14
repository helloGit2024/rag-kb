"""T12 验收脚本：上传前置校验（类型 / 大小 / md5 去重）。

用法：
    python scripts/check_file.py <文件> [--kb-id 1] [--register]

输出约定（诊断走 stderr，与项目其他脚本一致）：
    - 通过 → stdout 打印 `OK <md5>`（唯一一行，便于被下游脚本引用）；退出码 0；
    - 被拦 → stderr 打印结构化错误码（FILE_TYPE_UNSUPPORTED / FILE_TOO_LARGE /
      DUPLICATE_FILE）；退出码 1。

★ --register 说明：V0.1 的上传接口要到 T22 才有，现阶段没有真实"上传"动作。
  加 --register 时，脚本会在校验通过后往 documents 写一行**最小元数据记录**
  （status=pending，只含 kb_id / title / source_type / file_md5 / file_size），
  模拟"这个文件已经上传过了"。于是对同一文件再跑一次 --register，
  就会命中 DUPLICATE_FILE —— 这就是 409 场景的手工复现方式。
  ★ 只写最小元数据行，不做解析 / 分块 / 向量化（那些属 T13 入库编排）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 免 PYTHONPATH：把项目根加入 sys.path，脚本即可直接运行（与 ingest.py 同约定）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.repos.models import Document  # noqa: E402
from app.services.file_validator import (  # noqa: E402
    FileValidationError,
    validate_upload,
)


def load_env() -> dict[str, str]:
    """读项目根 .env（KEY=VALUE），忽略注释与空行。

    宿主运行把 DATABASE_URL 里的 compose 服务名 postgres 换成 localhost，
    容器内外共用同一份 .env 而不漂移（与 scripts/ingest.py 同约定）。
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


def main() -> None:
    parser = argparse.ArgumentParser(description="T12 验收：上传前置校验")
    parser.add_argument("file_path", help="待校验的 md / txt 文件路径")
    parser.add_argument("--kb-id", type=int, default=1, help="目标知识库 id（默认 1）")
    parser.add_argument(
        "--register",
        action="store_true",
        help="校验通过后写一行最小 documents 记录（模拟已上传，用于演示去重）",
    )
    args = parser.parse_args()

    path = Path(args.file_path)
    if not path.is_file():
        sys.exit(f"文件不存在：{path}")

    env = load_env()
    database_url = env.get("DATABASE_URL")
    if not database_url:
        sys.exit("DATABASE_URL 未设置：请在项目根 .env 中配置")

    data = path.read_bytes()

    engine = create_engine(database_url)
    with Session(engine) as session:
        try:
            validated = validate_upload(
                path.name, data, kb_id=args.kb_id, session=session
            )
        except FileValidationError as e:
            # R7 显式失败：打印结构化错误码，退出码非零，不静默兜底
            print(f"✗ 校验未通过：{e.code}", file=sys.stderr)
            print(f"  message：{e.message}", file=sys.stderr)
            if e.detail:
                print(f"  detail ：{e.detail}", file=sys.stderr)
            sys.exit(1)

        print(f"✓ 校验通过（kb_id={args.kb_id}）", file=sys.stderr)
        print(f"  类型：{validated.source_type}（.{validated.ext}）", file=sys.stderr)
        print(f"  大小：{validated.file_size} 字节", file=sys.stderr)
        print(f"  md5 ：{validated.file_md5}", file=sys.stderr)

        if args.register:
            session.add(
                Document(
                    kb_id=args.kb_id,
                    title=validated.title,
                    source_type=validated.source_type,
                    file_md5=validated.file_md5,
                    file_size=validated.file_size,
                    status="pending",
                )
            )
            session.commit()
            print(
                "  已登记一行 documents（模拟已上传）——再跑一次会返回 DUPLICATE_FILE",
                file=sys.stderr,
            )

    print(f"OK {validated.file_md5}")


if __name__ == "__main__":
    main()
