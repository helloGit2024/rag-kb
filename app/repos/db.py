"""数据库连接装配（V0.1 · T15）。

职责：把"从环境变量取 DSN → 建 Engine → 产 Session"收敛到**一处**，
供检索（chunk_repo）与权限解析（api 依赖）复用。

★ R1：DSN（含密码）只从 `DATABASE_URL` 环境变量读取，代码里不留任何硬编码兜底。
★ 为什么放进 app/repos/：这是数据访问层，连接装配属于它的范畴；
  api 层只消费 `get_session()`，不自己拼连接串（目录约定）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """按 DATABASE_URL 建 Engine（进程内缓存一个，复用连接池）。

    R1：连接串只从环境变量取，代码里不留任何硬编码 DSN 兜底。
    本机跑测试/脚本时由 Makefile 从 .env 取值，并把 compose 服务名 postgres
    换成 localhost——与 scripts/ingest.py、scripts/seed_users.py 的约定一致。

    Raises:
        RuntimeError: 未设置 DATABASE_URL。**不兜底成某条默认连接串**（R7）。
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL 未设置（R1：不得在代码里写死连接串）。"
            "本机运行请用 `make test`，或先执行 `set -a && . ./.env && set +a`。"
        )
    return create_engine(url, pool_pre_ping=True)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：产出一个 Session（请求结束即关闭）。

    写成生成器依赖，交给 FastAPI 在请求生命周期内管理 open/close——
    这样 api 层无需手动 `with Session(...)`。
    """
    with sessionmaker(bind=get_engine())() as session:
        yield session
