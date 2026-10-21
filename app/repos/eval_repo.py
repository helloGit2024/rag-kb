"""评估三张表的数据访问（V0.1 · T33）。

★ app/repos/ 是唯一允许出现 SQL 的层（目录约定）。
本模块只做"写入 / 读取评估结果"，不做任何评估编排——
跑评估、算指标、判定 pass/fail 都是 T28 的 app/eval/ 的事。

★ 事务约定（与 task_repo 相同）：除特别标注外都不 commit，
  事务边界由调用方（T28 的评估编排）控制——一次 run 的
  "状态推进 + 逐条 item + 汇总 metrics"必须同生共死。
  例外：register_dataset / create_run 自带 commit，因为
  T28 跑评估前要先拿到 dataset_id / run_id 再开始长任务，
  中途崩溃时已拿到的 id 必须真实存在（R7 显式失败可定位）。

★ 两处"故意不拦、靠 DB 约束兜底"：
  - activate_dataset 不预检 unconfirmed_count：
    ck_active_requires_confirmed 会显式拒绝（IntegrityError），
    应用层重复检查只是多一次"检查与使用之间的竞态窗口"。
  - set_baseline 不预查既有基线：uq_run_baseline 部分唯一索引兜底，
    重复设置同样以 IntegrityError 显式失败。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.repos.models import EvalDataset, EvalRun, EvalRunItem


class DatasetNotFoundError(LookupError):
    """数据集 id 不存在（R7：显式失败，不返回 None 让调用方猜）。"""


class RunNotFoundError(LookupError):
    """评估 run id 不存在（R7：显式失败）。"""


def register_dataset(
    session: Session,
    *,
    name: str,
    version: str,
    repo_path: str,
    file_path: str | None,
    file_format: str,
    checksum: str,
    item_count: int,
    distribution: dict | None,
    tag_stats: dict | None,
    unconfirmed_count: int = 0,
    source: str = "manual",
) -> int:
    """注册一份数据集（state 恒为 draft），**自带 commit**，返回 dataset_id。

    ★ 为什么注册时不允许直接 active：active 只能走 activate_dataset，
      由 ck_active_requires_confirmed 约束兜底——未确认完的题目
      永远进不了 active（R13/R16）。
    """
    dataset = EvalDataset(
        name=name,
        version=version,
        repo_path=repo_path,
        file_path=file_path,
        file_format=file_format,
        checksum=checksum,
        item_count=item_count,
        distribution=distribution,
        tag_stats=tag_stats,
        unconfirmed_count=unconfirmed_count,
        source=source,
    )
    session.add(dataset)
    session.commit()
    return dataset.id


def activate_dataset(session: Session, dataset_id: int) -> None:
    """draft → active。unconfirmed_count>0 时由 ck_active_requires_confirmed
    约束抛出 IntegrityError（显式失败，R7）。"""
    dataset = get_dataset(session, dataset_id)
    dataset.state = "active"
    dataset.updated_at = datetime.now(UTC)
    session.commit()


def get_dataset(session: Session, dataset_id: int) -> EvalDataset:
    """按 id 取数据集；不存在抛 DatasetNotFoundError。"""
    dataset = session.execute(
        select(EvalDataset).where(EvalDataset.id == dataset_id)
    ).scalar_one_or_none()
    if dataset is None:
        raise DatasetNotFoundError(f"数据集不存在：dataset_id={dataset_id}")
    return dataset


def find_dataset_by_name_version(
    session: Session, *, name: str, version: str
) -> EvalDataset | None:
    """按 (name, version) 查数据集（uq_dataset_name_ver）；不存在返回 None。

    与 get_dataset 的分工：这里用于"注册前先看看在不在"（None 是正常分支），
    get_dataset 用于"必须存在、不在是异常"（R7 显式失败）。
    """
    return session.execute(
        select(EvalDataset).where(
            EvalDataset.name == name, EvalDataset.version == version
        )
    ).scalar_one_or_none()


def create_run(
    session: Session,
    *,
    dataset_id: int,
    git_commit: str,
    config_snapshot: dict,
    model_config_ids: list[int] | None,
    triggered_by: str = "manual",
) -> int:
    """建一条 running 状态的 run，**自带 commit**，返回 run_id。

    ★ git_commit / config_snapshot 必填（R14）：没有它们，两次评估的
      数字不可比——这是可复现性的地基，不允许省略。
    """
    run = EvalRun(
        dataset_id=dataset_id,
        git_commit=git_commit,
        config_snapshot=config_snapshot,
        model_config_ids=model_config_ids,
        status="running",
        triggered_by=triggered_by,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    session.commit()
    return run.id


def get_run(session: Session, run_id: int) -> EvalRun:
    """按 id 取 run；不存在抛 RunNotFoundError。"""
    run = session.execute(
        select(EvalRun).where(EvalRun.id == run_id)
    ).scalar_one_or_none()
    if run is None:
        raise RunNotFoundError(f"评估 run 不存在：run_id={run_id}")
    return run


def insert_run_items(session: Session, run_id: int, items: list[EvalRunItem]) -> None:
    """批量插入逐条结果。不 commit（由调用方统一控制事务）。

    ★ 重复 (run_id, item_id) 由 uq_run_item 显式拒绝——
      同一题在同一个 run 里出现两次只可能是编排层 bug，不静默覆盖。
    """
    session.add_all(items)
    session.flush()


def finish_run(
    session: Session,
    run_id: int,
    *,
    status: str,
    metrics: dict | None,
    attribution_counts: dict | None = None,
    cost_cny: Decimal | None = None,
    total_tokens_in: int | None = None,
    total_tokens_out: int | None = None,
    wall_time_s: int | None = None,
    report_path: str | None = None,
    error_reason: str | None = None,
) -> None:
    """收尾一个 run（done / failed / cancelled），写汇总指标与耗时。"""
    run = get_run(session, run_id)
    run.status = status
    run.metrics = metrics
    run.attribution_counts = attribution_counts
    run.cost_cny = cost_cny
    run.total_tokens_in = total_tokens_in
    run.total_tokens_out = total_tokens_out
    run.wall_time_s = wall_time_s
    run.report_path = report_path
    run.error_reason = error_reason
    run.finished_at = datetime.now(UTC)
    session.commit()


def set_baseline(session: Session, run_id: int) -> None:
    """把一个 run 标为基线。同数据集已有基线时由 uq_run_baseline
    部分唯一索引抛出 IntegrityError（显式失败，R7）。"""
    run = get_run(session, run_id)
    run.is_baseline = True
    session.commit()
