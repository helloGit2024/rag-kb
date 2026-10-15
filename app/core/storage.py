"""对象存储客户端（V0.1 · T11）。

职责：把文档原文与解析后文本存到 S3 兼容对象存储。
本项目的实现是 **silo**（MinIO 社区延续）——
MinIO 官方镜像已下架，理由与复核条件见 `docs/adr/0010`。

设计要点（每条对应一条硬规则或一条验收标准）：
- **桶私有**：不设置任何匿名访问策略，对象只能经后端访问（`docs/security.md`）。
- **key 约定**：一个文档一个前缀 `docs/{doc_uuid}/`，
  V1 实现"文档删除全链路清理"（T47）时按前缀整段删除即可。
- **密钥外置（R1）**：endpoint / 账号 / 密码 / 桶名全部从环境变量读取。
- **显式失败（R7）**：缺配置抛 RuntimeError，S3Error 直接向上抛；
  不吞异常、不用默认值兜底、不返回 None 冒充成功。
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass

from minio import Minio
from minio.error import S3Error

_DEFAULT_BUCKET = "ragkb"
_DEFAULT_CONTENT_TYPE = "text/plain; charset=utf-8"


@dataclass(frozen=True)
class StorageConfig:
    """从环境变量读到的对象存储配置。

    endpoint 存 `host:port` 形态（minio SDK 要求，不带 scheme）；
    secure 由原始 endpoint 的 scheme 推导，因此 `MINIO_ENDPOINT` 一个变量
    就能同时表达"连哪里"与"要不要 TLS"，不必再维护一个独立的开关。
    """

    endpoint: str
    secure: bool
    access_key: str
    secret_key: str
    bucket: str


def load_config() -> StorageConfig:
    """读环境变量并解析成 StorageConfig；缺失即显式报错（R7 / R1）。"""
    raw_endpoint = os.getenv("MINIO_ENDPOINT")
    if not raw_endpoint:
        raise RuntimeError(
            "MINIO_ENDPOINT 未设置：请在 .env 或环境变量中配置（形如 minio:9000）"
        )
    secure = raw_endpoint.startswith("https://")
    endpoint = raw_endpoint.removeprefix("https://").removeprefix("http://")
    endpoint = endpoint.rstrip("/")
    if not endpoint:
        raise RuntimeError(f"MINIO_ENDPOINT 格式不正确：{raw_endpoint!r}")

    access_key = os.getenv("MINIO_ROOT_USER")
    secret_key = os.getenv("MINIO_ROOT_PASSWORD")
    if not access_key or not secret_key:
        raise RuntimeError(
            "MINIO_ROOT_USER / MINIO_ROOT_PASSWORD 未设置：请在 .env 中配置"
        )

    return StorageConfig(
        endpoint=endpoint,
        secure=secure,
        access_key=access_key,
        secret_key=secret_key,
        bucket=os.getenv("MINIO_BUCKET") or _DEFAULT_BUCKET,
    )


def get_client(config: StorageConfig | None = None) -> Minio:
    """构造对象存储客户端。"""
    config = config or load_config()
    return Minio(
        config.endpoint,
        access_key=config.access_key,
        secret_key=config.secret_key,
        secure=config.secure,
    )


def ensure_bucket(
    *,
    client: Minio | None = None,
    config: StorageConfig | None = None,
) -> str:
    """确保桶存在（幂等），返回桶名。

    桶只建一次；已存在则不动（桶策略默认私有，不额外设置任何匿名访问）。
    """
    config = config or load_config()
    client = client or get_client(config)
    if not client.bucket_exists(config.bucket):
        try:
            client.make_bucket(config.bucket)
        except S3Error as e:
            # 竞态：另一个进程刚建好同名桶。这不是错误，其余错误照抛（R7）
            if e.code != "BucketAlreadyOwnedByYou":
                raise
    return config.bucket


def put_text(
    key: str,
    text: str,
    *,
    content_type: str = _DEFAULT_CONTENT_TYPE,
    client: Minio | None = None,
    config: StorageConfig | None = None,
) -> str:
    """把文本写入对象存储，返回对象 key。"""
    config = config or load_config()
    client = client or get_client(config)
    data = text.encode("utf-8")
    client.put_object(
        config.bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return key


def put_bytes(
    key: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
    client: Minio | None = None,
    config: StorageConfig | None = None,
) -> str:
    """把字节写入对象存储，返回对象 key（T18 暂存原文用——此时还没做 UTF-8 校验，
    不能假定能 decode 成 str）。"""
    config = config or load_config()
    client = client or get_client(config)
    client.put_object(
        config.bucket,
        key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )
    return key


def get_bytes(
    key: str,
    *,
    client: Minio | None = None,
    config: StorageConfig | None = None,
) -> bytes:
    """读出对象字节。连接必定释放（finally），异常照抛（R7）。"""
    config = config or load_config()
    client = client or get_client(config)
    response = client.get_object(config.bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def delete_object(
    key: str,
    *,
    client: Minio | None = None,
    config: StorageConfig | None = None,
) -> None:
    """删除单个对象。对象不存在不报错（minio SDK 语义，幂等）。"""
    config = config or load_config()
    client = client or get_client(config)
    client.remove_object(config.bucket, key)


def staging_key(task_uuid: str, filename: str) -> str:
    """上传暂存对象 key：`staging/{task_uuid}/{filename}`（T18）。

    与正式对象的 `docs/{doc_uuid}/` 前缀隔开：
    - 正式对象的生命周期跟着 documents 行走（删除时按前缀清）；
    - 暂存对象的生命周期跟着 parse_tasks 走（worker 消费后删），
      两个前缀不混，清理时不会误删。
    """
    return f"staging/{task_uuid}/{filename}"


def get_text(
    key: str,
    *,
    client: Minio | None = None,
    config: StorageConfig | None = None,
) -> str:
    """读出对象文本。连接必定释放（finally），异常照抛（R7）。"""
    config = config or load_config()
    client = client or get_client(config)
    response = client.get_object(config.bucket, key)
    try:
        return response.read().decode("utf-8")
    finally:
        response.close()
        response.release_conn()


def raw_key(doc_uuid: str, ext: str) -> str:
    """原文对象 key：`docs/{doc_uuid}/raw.{ext}`（ext 不带点）。"""
    return f"docs/{doc_uuid}/raw.{ext.lstrip('.')}"


def parsed_key(doc_uuid: str) -> str:
    """解析后文本对象 key：`docs/{doc_uuid}/parsed.txt`。"""
    return f"docs/{doc_uuid}/parsed.txt"


def delete_prefix(
    prefix: str,
    *,
    client: Minio | None = None,
    config: StorageConfig | None = None,
) -> int:
    """删除某前缀下的全部对象，返回删除的对象数。

    用途：入库事务提交失败时的**补偿**——对象已写进存储但 DB 行没落成，
    不清理就会留下"指向不存在文档"的孤儿对象。
    silo/MinIO 没有按前缀删除的接口，只能列出后逐个删；一个文档前缀下对象很少，
    量级完全可接受（V1 的文档删除全链路清理 T47 会复用本函数）。

    ★ 前缀下没有任何对象时返回 0、不报错：补偿路径必须幂等。
    """
    config = config or load_config()
    client = client or get_client(config)
    removed = 0
    for obj in client.list_objects(config.bucket, prefix=prefix, recursive=True):
        client.remove_object(config.bucket, obj.object_name)
        removed += 1
    return removed
