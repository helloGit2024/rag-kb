"""Redis 缓存装配（V0.1 · T16）：问答结果的**权限隔离**缓存。

★ R3：缓存 key **必须**含可见范围的指纹，禁止只用问题文本——
  否则 A 部门的问题会命中 B 部门的答案（越权泄露，且极难复现）。
  指纹由 `app/services/permission.scope_fingerprint()` 提供（顺序稳定），
  本模块只负责把「问题哈希 + 指纹」拼成 key，不认识权限语义（不反向依赖 services）。

★ R1：连接串只从 `REDIS_URL` 环境变量取，代码里不留任何硬编码兜底。

★ 缓存内容只含"答案 + 引用 + 命中块 id"，**不含**提示词与原文全文
  （api-data 规则第五节：禁止把全文文档内容写进日志/缓存）。
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from typing import Any

import redis

_CACHE_PREFIX = "qa"
_DEFAULT_TTL_S = 3600

# qa.py 据此把"缓存不可用"兜住并显式记日志（R7：不静默，但也不阻断问答）。
# 这里放的是"基础设施类"错误：Redis 连接/命令失败，以及 REDIS_URL 缺失。
CACHE_UNAVAILABLE_ERRORS = (redis.RedisError, RuntimeError)

# `qa` 日志里 `cache` 字段的取值（**T16 的验收方式就是看这个字段**）。
# ★ 三值而非两值：`miss`（读成功、确实没缓存）与 `error`（Redis 不可用、
#   根本没读成）必须分开——前者正常，后者是基础设施故障，处置完全不同。
#   混成一个值会让 T16 的验收**假绿**："B 权限不命中"会因为**谁都不命中**而恒真。
CACHE_HIT = "hit"
CACHE_MISS = "miss"
CACHE_ERROR = "error"


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    """按 `REDIS_URL` 建客户端（进程内单例，复用连接池）。

    R1：连接串只从环境变量取，代码里不留硬编码兜底。
    本机跑测试/脚本时由 Makefile 从 .env 取值，并把 compose 服务名 redis
    换成 localhost——与 Makefile `test` 目标里对 DATABASE_URL（`app/repos/db.py`）、
    MINIO_ENDPOINT（`app/core/storage.py`）的处理是同一套约定，三处缺一不可。
    （`db.py` 只管 DATABASE_URL，不负责 redis；别把归口记到它头上。）

    Raises:
        RuntimeError: 未设置 REDIS_URL。**不兜底成某条默认连接串**（R7）。
    """
    url = os.getenv("REDIS_URL")
    if not url:
        raise RuntimeError(
            "REDIS_URL 未设置（R1：不得在代码里写死连接串）。"
            "本机运行请用 `make test`，或先执行 `set -a && . ./.env && set +a`。"
        )
    return redis.Redis.from_url(url, decode_responses=True)


def qa_cache_key(question: str, scope_fingerprint: str) -> str:
    """组装问答缓存 key：问题文本 + 权限指纹**双维度**（R3）。

    - question：同一权限下不同问题必须分开；
    - scope_fingerprint：同一问题在不同可见范围下**必须**分开——这是越权隔离的核心。

    Args:
        question: 用户问题原文。
        scope_fingerprint: `permission.scope_fingerprint(scope_keys)` 的结果。

    Raises:
        ValueError: question 为空或指纹为空（R7 显式失败，不生成"谁都能命中"的 key）。
    """
    q = question.strip()
    if not q:
        raise ValueError("question 为空，无法生成缓存 key（R7）")
    if not scope_fingerprint:
        raise ValueError("scope_fingerprint 为空，拒绝生成缓存 key（R3/R7）")
    q_hash = hashlib.sha256(q.encode("utf-8")).hexdigest()
    return f"{_CACHE_PREFIX}:{q_hash}:{scope_fingerprint}"


def get_answer(key: str) -> dict[str, Any] | None:
    """读缓存。未命中返回 `None`（None = 未缓存，不是错误）。

    Raises:
        redis.RedisError: Redis 不可用。原样上抛，由调用方决定是否兜住（R7）。
    """
    raw = get_redis().get(key)
    return json.loads(raw) if raw else None


def set_answer(key: str, payload: dict[str, Any], *, ttl_s: int | None = None) -> None:
    """写缓存，带过期时间。

    ttl 取 `QA_CACHE_TTL_S`；未配置时用 `_DEFAULT_TTL_S`——这是**非密钥类配置的
    缺省值**，不是 R7 说的"错误兜底"（配置缺失 ≠ 运行出错）。

    Raises:
        redis.RedisError: Redis 不可用，原样上抛（R7）。
    """
    ttl = ttl_s if ttl_s is not None else _resolve_ttl()
    get_redis().set(key, json.dumps(payload, ensure_ascii=False), ex=ttl)


def _resolve_ttl() -> int:
    """从环境变量读缓存有效期（秒）。

    Raises:
        RuntimeError: 值不是整数。**不静默回落成默认值**——配置写错要让人看见（R7）。
    """
    raw = os.getenv("QA_CACHE_TTL_S")
    if not raw:
        return _DEFAULT_TTL_S
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"QA_CACHE_TTL_S 不是整数：{raw!r}（R7 显式失败）") from exc
