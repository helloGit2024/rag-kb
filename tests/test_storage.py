"""T11 对象存储集成测试。

★ 依赖：silo 容器必须在运行（`make dev` 拉起）。
  这是集成测试，不是单元测试——对象存储的读写必须用真实服务验证。

环境来源：由 `make test` 从 `.env` 载入，并把 compose 服务名 silo
换成 localhost（与 DATABASE_URL 同一套约定）。

验收：① put/get 往返内容一致；② 同一 doc_uuid 下原文与解析文本各成一个对象。
"""

from __future__ import annotations

import uuid

import pytest

from app.core.storage import (
    ensure_bucket,
    get_client,
    get_text,
    load_config,
    parsed_key,
    put_text,
    raw_key,
)


def _services_available() -> bool:
    """对象存储是否可达（不可达则跳过测试，而不是判失败）。"""
    try:
        config = load_config()
        get_client(config).bucket_exists(config.bucket)
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _services_available(),
    reason="silo 未启动或 .env 未配置对象存储，跳过存储集成测试",
)


class TestStorage:
    def test_put_get_roundtrip(self):
        """写入的内容读回来必须完全一致（含中文）。"""
        text = "T11 往返测试：中文内容必须原样读回。"
        key = f"tests/{uuid.uuid4()}/roundtrip.txt"
        client = get_client()
        bucket = ensure_bucket(client=client)
        try:
            put_text(key, text, client=client)
            assert get_text(key, client=client) == text
        finally:
            # 测完清理，不给验收环境留垃圾对象
            client.remove_object(bucket, key)

    def test_raw_and_parsed_are_two_objects(self):
        """同一 doc_uuid 下，原文与解析文本是两个独立对象。"""
        doc_uuid = str(uuid.uuid4())
        key_raw = raw_key(doc_uuid, "md")
        key_parsed = parsed_key(doc_uuid)
        client = get_client()
        bucket = ensure_bucket(client=client)
        try:
            put_text(key_raw, "# 标题\n正文内容。", client=client)
            put_text(key_parsed, "标题\n正文内容。", client=client)
            names = {
                obj.object_name
                for obj in client.list_objects(bucket, prefix=f"docs/{doc_uuid}/")
            }
            assert names == {key_raw, key_parsed}
        finally:
            for key in (key_raw, key_parsed):
                client.remove_object(bucket, key)
