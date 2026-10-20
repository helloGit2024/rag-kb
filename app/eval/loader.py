"""评估集加载 / 校验 / 注册（V0.1 · T28）。

职责（编排层，不写 SQL——数据访问走 app/repos/eval_repo）：
  1. 读 evals/golden_set.yaml（题目本体是文件，R16），按版本过滤
     （V0.1 = Q001–Q030 共 30 题）；
  2. 轻量校验（eval-lint 口径）：id 唯一、category 合法、refuse 题
     should_refuse 必须为 true、身份映射可解析——任一不满足**拒绝开始**（R7）；
  3. 数据集注册：sha256 checksum 落 eval_datasets；已注册但 checksum 不一致
     **拒绝开始**（R14：题变了必须人工确认后注册新版本，不静默接着跑）；
  4. scope_keys 字段 → 评估账号（X-KB-User）的身份映射（见 IDENTITY_MAP）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.repos import eval_repo

CATEGORIES = frozenset({"normal", "refuse", "permission", "edge"})

DATASET_NAME = "golden-v1-30"
DATASET_VERSION = "1.0.0"
REPO_PATH = "evals/golden_set.yaml"

# golden_set 的 scope_keys 字段 → 评估专用账号（scripts/seed_users.py 已灌库）。
# 该字段的含义是"该题设定的提问者可见范围"：
#   · dept:X → 对应部门的**干净**账号（erin 无跨部门授权；bob 带 dept:销售部
#     授权，拿 bob 跑"研发问销售必须被拒"的越权题会假失败——见 seed_users.py）；
#   · public / internal → 全员可见（permission._ALWAYS_VISIBLE），用哪个账号
#     结果都一样，统一用 erin（不带任何额外授权的干净身份）。
# 未在表内的取值显式报错（R7），不兜底成某个默认账号（默认账号 = 隐式越权入口）。
IDENTITY_MAP = {
    "public": "erin",
    "internal": "erin",
    "dept:研发部": "erin",
    "dept:销售部": "alice",
    "dept:市场部": "carol",
    "dept:财务部": "dave",
}


class GoldenSetError(ValueError):
    """评估集文件不合法或版本/checksum 不匹配（R7：显式失败，拒绝开始）。"""


@dataclass(frozen=True)
class GoldenItem:
    """一条评估题目（字段与 golden_set.yaml 头注释的字段说明一一对应）。"""

    item_id: str
    category: str
    question: str
    expected_answer: str
    expected_sources: list[str] = field(default_factory=list)
    should_refuse: bool = False
    scope_keys: str = "public"
    notes: str = ""

    @property
    def username(self) -> str:
        """该题提问者的评估账号（X-KB-User）。"""
        try:
            return IDENTITY_MAP[self.scope_keys.strip()]
        except KeyError as exc:
            raise GoldenSetError(
                f"{self.item_id} 的 scope_keys={self.scope_keys!r} 不在身份映射表内"
            ) from exc

    @property
    def expected_doc_titles(self) -> list[str]:
        """标准来源的文档名（'员工手册.txt#年假章节' → '员工手册.txt'）。

        Recall@5 的判定只看文档级命中；块级章节锚点（# 后部分）留人工核对。
        """
        return [s.split("#", 1)[0].strip() for s in self.expected_sources if s.strip()]


def file_checksum(path: Path) -> str:
    """评估集文件的 sha256（十六进制）。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_golden_set(path: Path, *, version: str) -> list[GoldenItem]:
    """读 yaml 并按 version 过滤，返回校验通过、按 id 排序的题目列表。

    Raises:
        GoldenSetError: 文件结构非法 / 校验不通过（拒绝开始，R7/R13）。
    """
    if not path.exists():
        raise GoldenSetError(f"评估集文件不存在：{path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise GoldenSetError(f"评估集顶层必须是题目列表，实际是 {type(raw).__name__}")

    items = [
        _parse_item(entry)
        for entry in raw
        if isinstance(entry, dict) and str(entry.get("version", "")).strip() == version
    ]
    if not items:
        raise GoldenSetError(f"评估集中没有 version={version!r} 的题目")
    _validate(items)
    return sorted(items, key=lambda x: x.item_id)


def ensure_dataset(
    session: Session,
    *,
    path: Path,
    items: list[GoldenItem],
    name: str = DATASET_NAME,
    version: str = DATASET_VERSION,
) -> int:
    """注册或校验数据集，返回 dataset_id。

    - 未注册：register（draft）→ activate（unconfirmed_count=0，
      ck_active_requires_confirmed 约束放行）；
    - 已注册且 checksum 一致：直接复用；
    - 已注册但 checksum 不一致：**拒绝开始**（R14）——题变了必须人工确认后
      提高版本号重新注册；配置快照不同的两次运行不得直接宣称"进步了"。

    ★ 为什么不做"checksum 变了就自动升版本号"：版本号是人类语义
      （哪 30 题、标准答案谁确认的），自动 +1 会让"这次跑的是哪份题"
      失去答案——而无法解读的数字比没有数字更危险（R14）。
    ★ name/version 可覆盖仅为了测试隔离（测试用 name 前缀清理，不碰正式
      数据集）；生产调用一律用默认值，保证"一份题一个名字"。
    """
    checksum = file_checksum(path)
    existing = eval_repo.find_dataset_by_name_version(
        session, name=name, version=version
    )
    if existing is not None:
        if existing.checksum != checksum:
            raise GoldenSetError(
                "评估集 checksum 不一致（R14，拒绝开始）：\n"
                f"  库里：{existing.checksum}\n  文件：{checksum}\n"
                "  题目文件已被修改。请人工确认改动后，提高数据集版本号重新注册。"
            )
        if existing.state != "active":
            eval_repo.activate_dataset(session, existing.id)
        return existing.id

    distribution: dict[str, int] = {}
    for item in items:
        distribution[item.category] = distribution.get(item.category, 0) + 1
    dataset_id = eval_repo.register_dataset(
        session,
        name=name,
        version=version,
        repo_path=REPO_PATH,
        file_path=None,
        file_format="yaml",
        checksum=checksum,
        item_count=len(items),
        distribution=distribution,
        tag_stats=None,
        unconfirmed_count=0,
    )
    eval_repo.activate_dataset(session, dataset_id)
    return dataset_id


def _parse_item(entry: dict) -> GoldenItem:
    missing = [
        k for k in ("id", "category", "question", "expected_answer") if k not in entry
    ]
    if missing:
        raise GoldenSetError(f"题目 {entry.get('id', '?')} 缺必填字段：{missing}")
    sources = entry.get("expected_sources") or []
    if not isinstance(sources, list):
        raise GoldenSetError(f"{entry['id']} 的 expected_sources 必须是列表")
    return GoldenItem(
        item_id=str(entry["id"]).strip(),
        category=str(entry["category"]).strip(),
        question=str(entry["question"]),
        expected_answer=str(entry["expected_answer"]),
        expected_sources=[str(s) for s in sources],
        should_refuse=bool(entry.get("should_refuse", False)),
        scope_keys=str(entry.get("scope_keys", "public")).strip(),
        notes=str(entry.get("notes", "")),
    )


def _validate(items: list[GoldenItem]) -> None:
    """轻量 eval-lint：任一不满足即拒绝开始（R7/R13）。"""
    seen: set[str] = set()
    for item in items:
        if item.item_id in seen:
            raise GoldenSetError(f"题目 id 重复：{item.item_id}")
        seen.add(item.item_id)
        if item.category not in CATEGORIES:
            raise GoldenSetError(
                f"{item.item_id} 的 category={item.category!r} 不在枚举内"
                f" {sorted(CATEGORIES)}"
            )
        if item.category == "refuse" and not item.should_refuse:
            raise GoldenSetError(
                f"{item.item_id} 是 refuse 类但 should_refuse 不是 true"
            )
        # 身份映射此刻就验掉，避免跑到一半才在某一题上炸（R7：错得起始时间可定位）
        _ = item.username
