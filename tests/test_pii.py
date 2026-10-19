"""T24 PII 输出脱敏的测试。

两层覆盖：
- redact_pii（纯单元）：手机号 / 身份证各格式与边界，普通数字不误伤；
- 生成链路（沿用 T07 的 MockTransport 模式）：
  ① 进模型前——捕获发给网关的请求体，断言完整手机号不在其中
    （constraints.md 数据合规：模型只接收脱敏后的片段）；
  ② 出系统前——snippet 脱敏（原文直切必须脱）、答案兜底脱敏；
  ③ 与 R6 的交互——模型照抄掩码号码不误判拒答（头尾数字是原文子串）。

为什么"答案不含完整手机号"敢写成确定性断言：
输入侧脱敏后模型物理上没见过完整号码（ deterministic 防线），
不依赖模型发挥，e2e 同理。
"""

from __future__ import annotations

import json

import httpx

from app.repos.chunk_repo import ChunkHit
from app.services.generator import generate_answer
from app.services.pii import redact_pii


class TestRedactPii:
    """单元：各格式掩码与不误伤边界。"""

    def test_phone_masked(self) -> None:
        assert redact_pii("联系人张三，电话 13812345678。") == (
            "联系人张三，电话 138****5678。"
        )

    def test_multiple_phones(self) -> None:
        assert redact_pii("13900001111 或 13711112222") == "139****1111 或 137****2222"

    def test_phone_preserve_length(self) -> None:
        masked = redact_pii("13812345678")
        assert masked == "138****5678"
        assert len(masked) == 11

    def test_invalid_prefix_untouched(self) -> None:
        # 12x 不是手机号段（1[3-9]），原样保留
        assert redact_pii("编号 12345678901") == "编号 12345678901"

    def test_phone_inside_longer_digits_untouched(self) -> None:
        # 16 位连续数字（如合同编号）不得被切出 11 位误伤（数字边界保护）
        assert redact_pii("单号 1234567890123456") == "单号 1234567890123456"

    def test_id_card_masked(self) -> None:
        assert redact_pii("身份证 110101199003077758") == "身份证 1101**********7758"

    def test_id_card_with_x(self) -> None:
        assert redact_pii("11010119900307775X") == "1101**********775X"

    def test_id_card_preserve_length(self) -> None:
        assert len(redact_pii("110101199003077758")) == 18

    def test_business_numbers_untouched(self) -> None:
        # R6 关心的金额/数量/日期不是 PII，一个数字都不能动
        text = "预算 1204 万元，年假 10 天，2026 年 10 月 19 日。"
        assert redact_pii(text) == text

    def test_empty_text(self) -> None:
        assert redact_pii("") == ""


# ── 生成链路（MockTransport，模式照抄 tests/test_generator.py）─────────
def _hits_with_phone() -> list[ChunkHit]:
    return [
        ChunkHit(
            chunk_id=31,
            content="行政部联系人张三，电话 13812345678，工作日可拨打。",
            doc_id=3,
            doc_title="行政通讯录.txt",
            char_start=0,
            char_end=24,
            score=0.71,
        ),
    ]


def _mock(content: str, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8},
            },
        )

    return httpx.MockTransport(handler)


class TestGeneratorRedaction:
    def test_model_input_is_redacted(self) -> None:
        """进模型前脱敏：发给网关的请求体里不得出现完整手机号。"""
        seen: list = []
        generate_answer(
            "行政部联系人电话是多少？",
            _hits_with_phone(),
            transport=_mock("行政部联系人电话是 138****5678[1]。", seen),
        )
        payload = str(seen)
        assert "13812345678" not in payload
        assert "138****5678" in payload

    def test_snippet_is_redacted(self) -> None:
        """引用 snippet 是原文直切，输出前必须脱敏。"""
        r = generate_answer(
            "行政部联系人电话是多少？",
            _hits_with_phone(),
            transport=_mock("行政部联系人电话是 138****5678[1]。"),
        )
        assert len(r.citations) == 1
        assert "13812345678" not in r.citations[0].snippet
        assert "138****5678" in r.citations[0].snippet

    def test_answer_backstop_redaction(self) -> None:
        """兜底：即使模型输出了完整号码（如 mock 注入），答案也被脱敏。

        完整号码在被引原文里逐字存在，R6 不会拦——这道兜底必须有。
        """
        r = generate_answer(
            "行政部联系人电话是多少？",
            _hits_with_phone(),
            transport=_mock("行政部联系人电话是 13812345678[1]。"),
        )
        assert r.refused is False
        assert "13812345678" not in r.answer
        assert "138****5678" in r.answer

    def test_masked_number_passes_r6(self) -> None:
        """R6 交互：模型照抄掩码号码不得误判拒答——
        掩码头尾数字（138/5678）是原文 13812345678 的子串。"""
        r = generate_answer(
            "行政部联系人电话是多少？",
            _hits_with_phone(),
            transport=_mock("电话是 138****5678[1]。"),
        )
        assert r.refused is False
        assert r.answer == "电话是 138****5678[1]。"

    def test_refusal_unaffected(self) -> None:
        """拒答路径不经过脱敏也无碍：固定句式无 PII。"""
        from app.services.generator import REFUSAL_TEXT

        r = generate_answer(
            "火星上有什么？",
            _hits_with_phone(),
            transport=_mock(REFUSAL_TEXT),
        )
        assert r.refused is True
        assert r.answer == REFUSAL_TEXT
