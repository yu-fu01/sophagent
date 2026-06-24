"""/compact 命令测试：引导式 focus + 引擎复用。"""

from typing import AsyncIterator

import pytest

from sophagent.agent.commands.builtin import compact as cmd
from sophagent.agent.compaction import SUMMARY_PREFIX
from sophagent.models import AssistantTurn, Message, StreamEvent


class _FakeProvider:
    def __init__(self):
        self.calls = []

    async def chat(self, *, model, system, messages, tools=None,
                   temperature=None, max_tokens=None, thinking=None) -> AsyncIterator[StreamEvent]:
        self.calls.append(messages)
        turn = AssistantTurn(content="结构化摘要", input_tokens=1, output_tokens=1,
                             cache_read_tokens=0, cache_write_tokens=0)
        yield StreamEvent("turn_done", turn=turn)


@pytest.fixture
def fake_compact_ctx():
    class FakeDB:
        def __init__(self, messages):
            self._messages = messages
            self.compacted_history = None

        async def load_messages(self, sid):
            return list(self._messages)

        async def get_agent(self, aid):
            return {"provider": "test", "model": "m"}

        async def compact_session(self, sid, history):
            self.compacted_history = history

    def make(num_messages: int):
        msgs = [Message(role="user" if i % 2 == 0 else "assistant",
                        content=("内容" * 1500) + str(i)) for i in range(num_messages)]
        db = FakeDB(msgs)
        session = {"id": "s1", "agent_id": 1,
                   "override_provider": None, "override_model": None}
        return {"db": db, "session": session, "user": {"id": 1}}

    return make


@pytest.mark.asyncio
async def test_compact_with_focus_passes_focus_into_prompt(monkeypatch, fake_compact_ctx):
    """/compact 鉴权模块 → 摘要 prompt 含 focus 关键词。"""
    provider = _FakeProvider()
    monkeypatch.setattr("sophagent.providers.get_provider", lambda name: provider)
    ctx = fake_compact_ctx(num_messages=8)
    res = await cmd.handle("鉴权模块", ctx)
    assert res["action"] == "reload"
    sent = provider.calls[0][0].content
    assert "鉴权模块" in sent
    # 写回 DB 的新历史首条是带前缀的摘要
    new_history = ctx["db"].compacted_history
    assert new_history[0].content.startswith(SUMMARY_PREFIX)


@pytest.mark.asyncio
async def test_compact_too_few_messages(fake_compact_ctx):
    ctx = fake_compact_ctx(num_messages=3)
    res = await cmd.handle("", ctx)
    assert "too few" in res["content"].lower() or "少" in res["content"]
