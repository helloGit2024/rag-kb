"""任务失败原因的"人话化"映射（V0.1 · T19）。

为什么需要这一层：`error_reason` 是**给人看的**（`docs/api-contract.md` §5.4、
`openapi.yaml` GET /api/v1/tasks 的描述、`docs/data-model.md` 2.3 "必须是人话"）。
直接把 `f"{type(exc).__name__}: {exc}"` 落库会得到 `IngestError: ...` 这种
"给机器看"的字串——3000 份文档必然有几十份失败，看不懂原因就只能人肉逐个排查
（api-contract 5.4 的原话）。

★ 边界：本模块**只做文案映射**，不改写原始错误、不吞异常（R7）。
  真实的异常链仍由 worker 记录到日志（日志给机器看，error_reason 给人看）。
★ 也是 T23 状态查询接口复用的人话来源——避免同一个映射在 worker 与 api 里写两遍。
"""

from __future__ import annotations

from app.core.embedding import EmbeddingError
from app.services.file_validator import FileValidationError
from app.services.ingest import IngestError
from app.services.permission import ScopeError

# 兜底文案前缀：未知异常也必须给出"这是什么 + 下一步做什么"，而不是裸堆栈
_UNKNOWN_PREFIX = "处理失败"


def human_reason(exc: BaseException, *, retried: int = 0) -> str:
    """把异常翻译成一句给人看的中文（含"建议"），可选标注已重试次数。

    Args:
        exc: worker 侧捕获到的原始异常。
        retried: 重试耗尽时的已重试次数；> 0 时在句末追加说明，
            让用户知道"系统已经自己试过了"，而不是简单地失败一次。

    Returns:
        中文句子。**不以异常类名开头**（验收：error_reason 是人话）。

    ★ 不抛异常、不返回空串：任何入参都要有一句可读的话（含兜底分支）。
    """
    base = _base_message(exc)
    if retried > 0:
        return f"{base}（已自动重试 {retried} 次仍失败）"
    return base


def _base_message(exc: BaseException) -> str:
    """按异常类型给基础文案；顺序有意义（子类/具体类型在前）。"""
    if isinstance(exc, FileValidationError):
        # T12 的校验异常自带契约错误码与中文 message，直接复用，不重复造句
        return exc.message
    if isinstance(exc, ScopeError):
        return f"文档可见范围取值不合法：{exc}。建议：重新上传并选择正确的可见范围。"
    if isinstance(exc, UnicodeDecodeError):
        return (
            "文件不是 UTF-8 编码，无法解析。"
            "建议：用记事本等工具另存为 UTF-8 后再上传。"
        )
    if isinstance(exc, IngestError):
        # ingest 编排抛出的 IngestError 已是中文句子（编码 / 空内容 / 向量数不符 /
        # 补偿失败），这里只补一句"下一步做什么"，避免把技术细节再包装一层
        return f"{exc}。建议：修正文件内容后重新上传；若反复失败请提供 task_id 排查。"
    if isinstance(exc, EmbeddingError):
        return (
            f"嵌入服务不可用，向量化未能完成：{exc}。"
            "建议：确认嵌入服务（Ollama/TEI）已启动并加载模型后重新上传。"
        )
    # 兜底：保留原始信息便于排查，但加一句人话开头，且**不**带异常类名前缀
    return f"{_UNKNOWN_PREFIX}：{exc}。建议：稍后重试；反复失败请提供 task_id 排查。"
