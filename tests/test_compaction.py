"""上下文压缩引擎测试。"""

from typing import AsyncIterator

import pytest

from sophclaw.agent import compaction as C
from sophclaw.models import AssistantTurn, Message, StreamEvent, ToolCall


def test_make_summary_message_has_prefix_and_flag():
    m = C.make_summary_message("正文")
    assert m.compressed is True
    assert m.content.startswith(C.SUMMARY_PREFIX)
    assert "正文" in m.content


def test_is_summary_message_detects_flag_and_legacy_prefix():
    assert C.is_summary_message(C.make_summary_message("x")) is True
    assert C.is_summary_message(Message(role="user", content=C.LEGACY_SUMMARY_PREFIX + " 旧")) is True
    assert C.is_summary_message(Message(role="user", content="普通消息")) is False


def test_find_previous_summary_strips_wrappers():
    msgs = [
        Message(role="user", content="早"),
        C.make_summary_message("上一份摘要正文"),
        Message(role="user", content="新问题"),
    ]
    assert C.find_previous_summary(msgs) == "上一份摘要正文"


def test_serialize_turns_includes_tool_calls_and_results():
    turns = [
        Message(role="user", content="读下配置"),
        Message(role="assistant", content="好的",
                tool_calls=[ToolCall(id="1", name="read_file", arguments={"path": "config.py"})]),
        Message(role="tool", content="文件内容很长" * 10, tool_call_id="1"),
    ]
    s = C.serialize_turns(turns)
    assert "read_file" in s
    assert "config.py" in s
    assert "读下配置" in s


def test_split_for_summary_protects_tail_and_keeps_tool_with_call():
    msgs = [Message(role="user", content="x" * 300) for _ in range(6)]
    msgs[-1] = Message(role="tool", content="结果", tool_call_id="1")
    old, rest = C.split_for_summary(msgs, tail_budget_tokens=200, min_protect=2)
    assert len(old) + len(rest) == len(msgs)
    assert len(rest) >= 2
    # tail 不以孤立的 tool 结果开头
    assert rest[0].role != "tool"


def test_build_summary_prompt_first_vs_iterative_and_focus():
    turns = [Message(role="user", content="做点事")]
    sys1, user1 = C.build_summary_prompt(turns, prev_summary=None, focus=None, today="2026-06-24")
    assert "REDACTED" in sys1
    assert "## 活动任务" in user1
    assert "2026-06-24" in user1  # 温度锚定
    sys2, user2 = C.build_summary_prompt(turns, prev_summary="老摘要", focus="鉴权模块", today="2026-06-24")
    assert "老摘要" in user2          # 迭代更新引用上一份
    assert "鉴权模块" in user2        # focus 注入


class _FakeProvider:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def chat(self, *, model, system, messages, tools=None,
                   temperature=None, max_tokens=None, thinking=None) -> AsyncIterator[StreamEvent]:
        self.calls.append((system, messages))
        turn = AssistantTurn(content=self.text, input_tokens=1, output_tokens=1,
                             cache_read_tokens=0, cache_write_tokens=0)
        yield StreamEvent("turn_done", turn=turn)


@pytest.mark.asyncio
async def test_summarize_returns_text():
    p = _FakeProvider("结构化摘要")
    out = await C.summarize(p, "m", [Message(role="user", content="hi")])
    assert out == "结构化摘要"


@pytest.mark.asyncio
async def test_summarize_returns_none_on_error():
    class Boom:
        async def chat(self, **kw):
            raise RuntimeError("boom")
            yield  # pragma: no cover
    out = await C.summarize(Boom(), "m", [Message(role="user", content="hi")])
    assert out is None


# -- 凭据脱敏接入（入口 serialize_turns + 出口 summarize）--------------------

from typing import AsyncIterator as _AI


def test_serialize_turns_redacts_credentials():
    turns = [
        Message(role="user", content="我的 key 是 sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c"),
        Message(role="user", content="连接串 postgres://admin:Pa55w0rd!2026@db.internal:5432/x"),
        Message(role="user", content="还有 AKIA1234567890SOPHNET"),
    ]
    s = C.serialize_turns(turns)
    assert "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c" not in s
    assert "Pa55w0rd!2026" not in s
    assert "AKIA1234567890SOPHNET" not in s
    assert "[REDACTED]" in s


class _LeakyProvider:
    """模拟 LLM 在摘要里回吐了凭据。"""
    async def chat(self, *, model, system, messages, tools=None,
                   temperature=None, max_tokens=None, thinking=None) -> _AI:
        leaked = ("## 关键上下文\nAPI key 是 sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c，"
                  "连接串 postgres://admin:Pa55w0rd!2026@db.internal:5432/x，"
                  "还有 AKIA1234567890SOPHNET")
        turn = AssistantTurn(content=leaked, input_tokens=1, output_tokens=1)
        yield StreamEvent("turn_done", turn=turn)


@pytest.mark.asyncio
async def test_summarize_redacts_leaked_credentials_in_output():
    out = await C.summarize(_LeakyProvider(), "m", [Message(role="user", content="hi")])
    assert out is not None
    assert "sk-test-DO-NOT-COMMIT-9f8a7b6c5d4e3f2a1b0c" not in out
    assert "Pa55w0rd!2026" not in out
    assert "AKIA1234567890SOPHNET" not in out
    assert "[REDACTED]" in out
