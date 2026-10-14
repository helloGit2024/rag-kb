"""T11 验收脚本：把一份文档的原文与解析文本写入对象存储。

用法：
    python scripts/store_file.py <md 或 txt 文件路径> [--doc-uuid <自定义 uuid>]

输出约定（诊断走 stderr，与项目其他脚本一致）：
    - 两个对象的 key、被删行数、控制台地址 → stderr（供你到浏览器核对）；
    - 原文对象 key → stdout（唯一一行，便于被下游脚本引用）。

★ 范围说明：本脚本只为验收"原文与解析文本能各存成一个对象"，
  不落库、不分块 —— 正式的入库编排是 T13（app/services/ + worker）。

解析器说明：V0.1 目前只有 markdown 解析器（T10），其规则
（页码行、重复短行、空行归一化）是**按行**处理的、不依赖 markdown 语法，
因此 txt 也走同一套规则；后续若有独立 txt 解析器再分流。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

# 免 PYTHONPATH：把项目根加入 sys.path，脚本即可直接运行（与 parse_md.py 同约定）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.storage import (  # noqa: E402
    ensure_bucket,
    get_client,
    parsed_key,
    put_text,
    raw_key,
)
from app.services.md_parser import parse_markdown  # noqa: E402


def load_env() -> dict[str, str]:
    """读项目根 .env（KEY=VALUE），忽略注释与空行。

    把 endpoint 里的 compose 服务名 silo 替换为 localhost ——
    宿主脚本与容器共用同一份 .env 而不漂移（与 scripts/ingest.py 同约定）。
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
    endpoint = env.get("MINIO_ENDPOINT", "")
    if "://silo:" in endpoint:
        env["MINIO_ENDPOINT"] = endpoint.replace("://silo:", "://localhost:")
    return env


def resolve_source(path: Path) -> tuple[str, str, str]:
    """由文件后缀决定 (ext, source_type, content_type)。

    V0.1 只支持 md / txt；其余类型显式报错（R7），不做静默兜底。
    """
    ext = path.suffix.lstrip(".").lower()
    if ext in ("md", "markdown"):
        return "md", "markdown", "text/markdown; charset=utf-8"
    if ext == "txt":
        return "txt", "txt", "text/plain; charset=utf-8"
    raise ValueError(f"不支持的文件类型：.{ext}（V0.1 只支持 md / txt）")


def main() -> None:
    parser = argparse.ArgumentParser(description="T11 验收：原文 + 解析文本落对象存储")
    parser.add_argument("file_path", help="待存储的 md / txt 文件路径")
    parser.add_argument("--doc-uuid", help="自定义文档 uuid（默认随机生成）")
    args = parser.parse_args()

    path = Path(args.file_path)
    if not path.is_file():
        sys.exit(f"文件不存在：{path}")

    try:
        ext, source_type, content_type = resolve_source(path)
    except ValueError as e:
        sys.exit(str(e))

    env = load_env()
    if not env:
        sys.exit(".env 不存在或为空：请先 cp .env.example .env 并填写")
    # storage.py 统一从环境变量取值，因此把 .env 灌进去，避免两套来源
    os.environ.update(env)

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        sys.exit(f"读取失败：文件不是 UTF-8 编码（{e}）。请先转为 UTF-8 再存储。")

    doc_uuid = args.doc_uuid or str(uuid.uuid4())
    key_raw = raw_key(doc_uuid, ext)
    key_parsed = parsed_key(doc_uuid)

    client = get_client()
    bucket = ensure_bucket(client=client)  # 幂等；桶默认私有，不设匿名访问

    put_text(key_raw, text, content_type=content_type, client=client)
    result = parse_markdown(text)
    put_text(key_parsed, result.text, client=client)

    print(f"桶：{bucket}", file=sys.stderr)
    print(f"文档类型：{source_type}（doc_uuid={doc_uuid}）", file=sys.stderr)
    print(f"原文对象：{key_raw}（{len(text)} 字）", file=sys.stderr)
    print(f"解析对象：{key_parsed}（{len(result.text)} 字）", file=sys.stderr)
    print(f"被删页眉页脚行：{len(result.removed_lines)} 条", file=sys.stderr)
    console_port = os.getenv("MINIO_CONSOLE_PORT", "9001")
    print(f"控制台：http://localhost:{console_port}", file=sys.stderr)

    print(key_raw)


if __name__ == "__main__":
    main()
