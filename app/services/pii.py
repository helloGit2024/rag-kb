"""PII 输出脱敏（V0.1 · T24）。

依据（三处文档已核对一致）：
- `docs/security.md` 4.3：入库**不脱敏**（脱敏后语义变形、影响检索质量），
  只在**输出时**脱敏；手机号/身份证/银行卡 → `138****5678` 式；
  客户名称**不脱敏**（业务需要，靠权限隔离）。
- `docs/security.md` 合规验收表 + `docs/constraints.md` 数据合规（"这条最硬"）：
  生成模型只接收**脱敏后**的检索片段——所以脱敏是**双点**：
  ① 进模型前（`_build_context`）；② 出系统前（答案 + 引用 snippet）。
  输入侧是确定性防线：模型没见过完整号码，就不可能输出它。

V0.1 范围（与项目所有者确认）：**手机号 + 身份证**。
银行卡暂缓：语料本身不含（constraints.md L43），且 16—19 位裸数字的
银行卡正则会误伤语料里真实存在的合同号/编号（备案 OPEN-QUESTIONS）。

保长掩码（`138****5678` 仍是 11 位）的附带好处：R6 数字校验不受干扰——
掩码保留的头尾数字仍是原文子串，`_unverifiable_numbers` 不会误判。
"""

from __future__ import annotations

import re

# 手机号：1[3-9] 开头共 11 位；前后不得再挨数字（防止从更长数字串里切出 11 位误伤）
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 身份证：17 位数字 + 末位数字或 X；同样的数字边界保护
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


def _mask_phone(m: re.Match[str]) -> str:
    """`13812345678` → `138****5678`（security.md 4.3 给出的格式，保长 11 位）。"""
    s = m.group(0)
    return s[:3] + "****" + s[-4:]


def _mask_id_card(m: re.Match[str]) -> str:
    """18 位身份证 → 前 4 + 10 个 * + 后 4（保长 18 位）。"""
    s = m.group(0)
    return s[:4] + "*" * 10 + s[-4:]


def redact_pii(text: str) -> str:
    """把文本中的手机号 / 身份证号替换为保长掩码，返回新文本。

    纯函数，不抛异常：脱敏是输出管线的一环，绝不能因为一段文本
    把整条问答链路打挂（匹配不中就原样返回，这是安全方向）。
    先脱身份证（18 位更长），再脱手机号——顺序其实不影响结果
    （两套正则都有数字边界保护，不会互相切子串），长模式先是习惯。
    """
    if not text:
        return text
    text = _ID_CARD_RE.sub(_mask_id_card, text)
    return _PHONE_RE.sub(_mask_phone, text)
