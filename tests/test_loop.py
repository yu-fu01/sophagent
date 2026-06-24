"""AgentRunner loop tests using a scripted fake provider."""

from typing import AsyncIterator

import pytest

import sophclaw.providers as providers_mod
from sophclaw.agent.loop import AgentRunner, history_tokens, truncate_old_tool_messages
from sophclaw.config import ProviderConfig, get_config
from sophclaw.models import AssistantTurn, Message, StreamEvent, ToolCall


class FakeProvider:
    """Replays a scripted list of AssistantTurns."""

    def __init__(self, turns: list[AssistantTurn]):
        self.turns = list(turns)
        self.calls: list[list[Message]] = []

    async def chat(self, *, model, system, messages, tools=None,
                   temperature=None, max_tokens=None, thinking=None) -> AsyncIterator[StreamEvent]:
        self.calls.append([Message.from_dict(m.to_dict()) for m in messages])
        turn = self.turns.pop(0)
        if turn.reasoning:
            yield StreamEvent("reasoning_delta", text=turn.reasoning)
        if turn.content:
            yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("turn_done", turn=turn)


@pytest.fixture
def fake_provider(ctx, monkeypatch):
    def install(turns):
        provider = FakeProvider(turns)
        monkeypatch.setitem(providers_mod._cache, "test", provider)
        get_config().providers["test"] = ProviderConfig(name="test", api_mode="openai", context_limit=1000)
        return provider

    return install


async def collect(runner, user_input):
    return [ev async for ev in runner.run(user_input)]


def test_system_prompt_includes_model_identity(ctx):
    """The agent must know its own model name (BUG3.1): the system prompt injects
    the effective model/provider so it can answer "what model are you?"."""
    from sophclaw.agent.prompt import build_system_prompt

    prompt = build_system_prompt(ctx.agent)
    assert "test-model" in prompt   # ctx.agent.model
    assert "test" in prompt          # ctx.agent.provider


async def test_simple_turn(ctx, fake_provider):
    fake_provider([AssistantTurn(content="hello!", stop_reason="stop")])
    runner = AgentRunner(ctx.agent, ctx, history=[])
    events = await collect(runner, "hi")
    assert events[0] == {"type": "text_delta", "text": "hello!"}
    assert events[-1]["type"] == "done"
    assert [m.role for m in runner.history] == ["user", "assistant"]


async def test_reasoning_delta_forwarded(ctx, fake_provider):
    """Thinking-model reasoning is surfaced as a reasoning_delta UI event,
    kept out of the visible text, and still captured on the turn for echo-back."""
    fake_provider([AssistantTurn(content="the answer", reasoning="let me think", stop_reason="stop")])
    runner = AgentRunner(ctx.agent, ctx, history=[])
    events = await collect(runner, "hi")
    types = [e["type"] for e in events]
    # reasoning is emitted as its own event, before the visible answer
    assert {"type": "reasoning_delta", "text": "let me think"} in events
    assert types.index("reasoning_delta") < types.index("text_delta")
    # the visible text stream carries only the answer, not the thinking
    assert {"type": "text_delta", "text": "the answer"} in events
    # reasoning is still captured on the persisted assistant message (echo-back)
    assert runner.history[-1].reasoning == "let me think"


async def test_tool_loop(ctx, fake_provider):
    fake_provider([
        AssistantTurn(tool_calls=[ToolCall(id="c1", name="write_file",
                                           arguments={"path": "f.txt", "content": "data"})]),
        AssistantTurn(content="file written", stop_reason="stop"),
    ])
    runner = AgentRunner(ctx.agent, ctx, history=[])
    events = await collect(runner, "write a file")
    types = [e["type"] for e in events]
    assert types == ["turn_usage", "tool_call", "tool_result", "text_delta", "turn_usage", "done"]
    assert (ctx.workspace / "f.txt").read_text() == "data"
    # tool result was fed back to the model on the second call
    assert any(m.role == "tool" for m in runner.history)


async def test_max_iterations_guard(ctx, fake_provider):
    looping = [AssistantTurn(tool_calls=[ToolCall(id=f"c{i}", name="list_dir", arguments={})])
               for i in range(100)]
    fake_provider(looping)
    ctx.agent.max_iterations = 3
    runner = AgentRunner(ctx.agent, ctx, history=[])
    events = await collect(runner, "loop forever")
    assert events[-1].get("note") == "max_iterations reached"
    assert sum(1 for e in events if e["type"] == "tool_call") == 3


async def test_persist_callback(ctx, fake_provider):
    fake_provider([AssistantTurn(content="ok", stop_reason="stop")])
    saved = []

    async def on_persist(msgs):
        saved.extend(msgs)

    runner = AgentRunner(ctx.agent, ctx, history=[], on_persist=on_persist)
    await collect(runner, "hi")
    assert [m.role for m in saved] == ["user", "assistant"]


def test_truncate_old_tool_messages():
    msgs = []
    for i in range(12):
        msgs.append(Message(role="assistant", tool_calls=[ToolCall(id=f"c{i}", name="t", arguments={})]))
        msgs.append(Message(role="tool", content="x" * 1000, tool_call_id=f"c{i}"))
    out = truncate_old_tool_messages(msgs)
    truncated = [m for m in out if m.role == "tool" and "truncated" in m.content]
    intact = [m for m in out if m.role == "tool" and "truncated" not in m.content]
    assert len(intact) == 8 and len(truncated) == 4


async def test_compression_triggers_summary(ctx, fake_provider):
    # context_limit=1000 tokens -> budget 800; stuff history beyond it
    provider = fake_provider([
        # summarizer calls (compression loops until under budget), then the real answer
        AssistantTurn(content="summary of old stuff", stop_reason="stop"),
        AssistantTurn(content="tighter summary", stop_reason="stop"),
        AssistantTurn(content="even tighter summary", stop_reason="stop"),
        AssistantTurn(content="final answer", stop_reason="stop"),
    ])
    history = [Message(role="user" if i % 2 == 0 else "assistant", content="word " * 200)
               for i in range(8)]
    runner = AgentRunner(ctx.agent, ctx, history=history)
    events = await collect(runner, "continue")
    assert events[-1]["type"] == "done"
    assert any(is_summary_message(m) for m in runner.history)
    assert history_tokens("", runner.history) < 1000


from sophclaw.agent.compaction import (
    SUMMARY_PREFIX,
    is_summary_message,
    make_summary_message,
)
from sophclaw.config import DEFAULT_COMPRESS_THRESHOLD


def test_agentrunner_default_threshold(ctx, fake_provider):
    fake_provider([])  # registers a "test" provider so AgentRunner can resolve it
    r = AgentRunner(ctx.agent, ctx, history=[])
    assert r.compress_threshold == DEFAULT_COMPRESS_THRESHOLD


@pytest.mark.asyncio
async def test_iterative_compaction_reuses_previous_summary(ctx, fake_provider):
    # 历史里已有一份摘要 + 大量后续消息；触发压缩时应做「迭代更新」而非从头重摘
    big = "字" * 1500
    history = [make_summary_message("上一份摘要")] + [
        Message(role="user", content=big) for _ in range(6)
    ]
    # fake_provider 脚本：唯一一次 chat 是「摘要调用」，返回新摘要文本
    provider = fake_provider([
        AssistantTurn(content="合并后的新摘要", input_tokens=1, output_tokens=1),
    ])
    runner = AgentRunner(ctx.agent, ctx, history=history, compress_threshold=0.5)
    assert runner.context_limit == 1000  # 来自 fake_provider fixture
    await runner._compress_if_needed(system="sys")
    # 摘要调用的 user prompt 里应引用上一份摘要（迭代更新路径）
    assert any("上一份摘要" in m.content for call in provider.calls for m in call)
    # 压缩后历史以一条带前缀的摘要消息开头
    assert runner.history[0].content.startswith(SUMMARY_PREFIX)
    assert runner.compressed is True
