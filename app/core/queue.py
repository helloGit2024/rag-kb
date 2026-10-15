"""Redis 任务队列（V0.1 · T18）：入库任务的入队 / 出队。

形态：Redis list，`LPUSH` 入队、`BRPOP` 出队（阻塞式，单 worker 够用）。
★ 消息体只放 task_id（字符串）——任务参数快照在 parse_tasks.payload
  （data-model 2.6"便于重放"），不把大 payload 塞进 Redis：
  Redis 是"信号"，数据库是"事实"。worker 崩溃重启后队列里的 id 还能再消费。

★ 与 app/core/cache.py 的分工：cache 管"可删的性能优化"，本模块管"不能丢的
  任务信号"——共用一个 Redis 实例与 get_redis() 连接，但 key 前缀不同、
  故障语义完全不同（队列故障 = 任务丢失风险，必须显式失败 R7，
  不像缓存那样 best-effort 降级）。
"""

from __future__ import annotations

import redis

from app.core.cache import get_redis

# 队列 key。与 cache 的 "qa:" 前缀隔开，互不误删。
QUEUE_KEY = "queue:ingest"

# BRPOP 阻塞秒数：到时就返回 None 让主循环空转一圈——
# 这样 worker 能周期性检查退出标志（将来加优雅停机时用得着），
# 又不会真的忙等烧 CPU。
_POP_TIMEOUT_S = 5


def enqueue_ingest(task_id: int, *, client: redis.Redis | None = None) -> None:
    """把 task_id 入队（LPUSH）。

    Raises:
        redis.RedisError: Redis 不可用。★ 原样上抛——上传接口必须据此返回
            结构化错误（R7），绝不能"任务行建了但队列没进"还返回 202，
            那是静默丢任务。
    """
    (client or get_redis()).lpush(QUEUE_KEY, str(task_id))


def pop_ingest(
    *, client: redis.Redis | None = None, timeout_s: int = _POP_TIMEOUT_S
) -> int | None:
    """阻塞式取一个 task_id（BRPOP）；超时返回 None。

    Raises:
        ValueError: 消息体不是合法 task_id（队列被污染，显式失败 R7）。
        redis.RedisError: Redis 不可用，原样上抛。
    """
    result = (client or get_redis()).brpop(QUEUE_KEY, timeout=timeout_s)
    if result is None:
        return None
    _key, raw = result
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"队列 {QUEUE_KEY} 中出现非法 task_id：{raw!r}（R7 显式失败）"
        ) from exc
