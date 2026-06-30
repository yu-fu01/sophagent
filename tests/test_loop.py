"""AgentRunner loop tests using a scripted fake provider."""

from typing import AsyncIterator

import pytest

import sophagent.providers as providers_mod
from sophagent.agent.loop import AgentRunner, history_tokens, truncate_old_tool_messages
from sophagent.config import ProviderConfig, get_config
from sophagent.models import AssistantTurn, Message, StreamEvent, ToolCall


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
    from sophagent.agent.prompt import build_system_prompt

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


from sophagent.agent.compaction import (
    SUMMARY_PREFIX,
    is_summary_message,
    make_summary_message,
)
from sophagent.config import DEFAULT_COMPRESS_THRESHOLD


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


@pytest.mark.asyncio
async def test_compaction_fallback_keeps_prev_summary_on_failure(ctx, fake_provider, monkeypatch):
    """summarize 失败返回 None 时，应保留上一份摘要并保住尾部消息。"""
    async def _fail(*a, **k):
        return None
    monkeypatch.setattr("sophagent.agent.loop.summarize", _fail)

    big = "字" * 1500
    history = [make_summary_message("旧摘要正文")] + [
        Message(role="user", content=big) for _ in range(6)
    ]
    provider = fake_provider([])  # 不会真正调用摘要（已被 patch）
    runner = AgentRunner(ctx.agent, ctx, history=history, compress_threshold=0.5)
    assert runner.context_limit == 1000
    await runner._compress_if_needed(system="sys")

    # 失败兜底：首条仍是带前缀的摘要消息（用 prev 重新包裹），且 runner.compressed=True
    assert is_summary_message(runner.history[0])
    assert runner.history[0].content.startswith(SUMMARY_PREFIX)
    assert "旧摘要正文" in runner.history[0].content
    # 尾部消息没有全部丢失
    assert len(runner.history) >= 2
    assert runner.compressed is True


def test_fast_lookup_guide_injected_with_terminal(ctx):
    """启用 terminal 时注入快路径引导；未启用则不注入。"""
    from sophagent.agent.prompt import build_system_prompt

    ctx.agent.tools = ["terminal"]
    assert "Fast lookups" in build_system_prompt(ctx.agent)

    ctx.agent.tools = ["read_file"]
    assert "Fast lookups" not in build_system_prompt(ctx.agent)


def test_expand_parallel_passthrough():
    """普通工具调用原样返回。"""
    from sophagent.agent.loop import expand_parallel_calls

    calls = [ToolCall(id="a", name="web_search", arguments={"query": "x"})]
    assert expand_parallel_calls(calls) == calls


def test_expand_parallel_unwraps_both_key_forms():
    """multi_tool_use.parallel 展开为真实调用，兼容 recipient_name/name 两种键，
    剥离命名空间前缀，生成稳定派生 id。"""
    from sophagent.agent.loop import expand_parallel_calls

    parent = ToolCall(
        id="p",
        name="multi_tool_use.parallel",
        arguments={"tool_uses": [
            {"recipient_name": "functions.web_search", "parameters": {"query": "a"}},
            {"name": "web_fetch", "arguments": {"url": "u"}},
        ]},
    )
    out = expand_parallel_calls([parent])
    assert [c.name for c in out] == ["web_search", "web_fetch"]
    assert [c.id for c in out] == ["p.0", "p.1"]
    assert out[0].arguments == {"query": "a"}
    assert out[1].arguments == {"url": "u"}


def test_expand_parallel_malformed_passthrough():
    """tool_uses 缺失/非数组时保留原调用，交给 dispatch 报未知工具。"""
    from sophagent.agent.loop import expand_parallel_calls

    bad = ToolCall(id="p", name="multi_tool_use.parallel", arguments={})
    assert expand_parallel_calls([bad]) == [bad]


def test_expand_parallel_empty_tool_uses_keeps_parent():
    """tool_uses 为空列表 → 没有有效子调用 → 保留原父调用，保证 id 一致。"""
    from sophagent.agent.loop import expand_parallel_calls

    parent = ToolCall(id="p", name="multi_tool_use.parallel", arguments={"tool_uses": []})
    assert expand_parallel_calls([parent]) == [parent]


def test_expand_parallel_skips_nameless_use():
    """缺工具名的子项被跳过；只展开有效的那个，id 连续。"""
    from sophagent.agent.loop import expand_parallel_calls

    parent = ToolCall(
        id="p",
        name="multi_tool_use.parallel",
        arguments={"tool_uses": [
            {"parameters": {"x": 1}},  # 无 recipient_name/name → 跳过
            {"recipient_name": "web_search", "parameters": {"query": "a"}},
        ]},
    )
    out = expand_parallel_calls([parent])
    assert [c.name for c in out] == ["web_search"]
    assert out[0].id == "p.0"  # 连续编号，不受被跳过项影响
    assert out[0].arguments == {"query": "a"}


async def test_parallel_dispatch_preserves_order(ctx, fake_provider, monkeypatch):
    """并发执行多个工具，但 history 中 tool 消息按原调用顺序排列（非完成顺序）。"""
    import asyncio

    from sophagent.agent import loop as loop_mod

    async def fake_dispatch(name, args, c):
        await asyncio.sleep(args.get("delay", 0.0))
        return f"done:{name}"

    monkeypatch.setattr(loop_mod.registry, "dispatch", fake_dispatch)
    turn = AssistantTurn(content="", stop_reason="tool_calls", tool_calls=[
        ToolCall(id="a", name="slow", arguments={"delay": 0.05}),
        ToolCall(id="b", name="fast", arguments={"delay": 0.0}),
    ])
    fake_provider([turn, AssistantTurn(content="ok", stop_reason="stop")])
    runner = AgentRunner(ctx.agent, ctx, history=[])
    await collect(runner, "go")
    tool_msgs = [m for m in runner.history if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["a", "b"]
    assert tool_msgs[0].content == "done:slow"


async def test_parallel_pseudo_tool_end_to_end(ctx, fake_provider, monkeypatch):
    """模型发来的 multi_tool_use.parallel 被拆解；assistant 消息与 tool 回复 id 一致。"""
    from sophagent.agent import loop as loop_mod

    async def fake_dispatch(name, args, c):
        return f"ran:{name}"

    monkeypatch.setattr(loop_mod.registry, "dispatch", fake_dispatch)
    turn = AssistantTurn(content="", stop_reason="tool_calls", tool_calls=[
        ToolCall(id="p", name="multi_tool_use.parallel", arguments={"tool_uses": [
            {"recipient_name": "web_search", "parameters": {"query": "a"}},
            {"recipient_name": "web_fetch", "parameters": {"url": "u"}},
        ]}),
    ])
    fake_provider([turn, AssistantTurn(content="ok", stop_reason="stop")])
    runner = AgentRunner(ctx.agent, ctx, history=[])
    await collect(runner, "go")
    tool_msgs = [m for m in runner.history if m.role == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["p.0", "p.1"]
    assert tool_msgs[0].content == "ran:web_search"
    # 关键：assistant 消息的 tool_calls 也应是展开后的 id，与 tool 回复一一对应
    assistant = next(m for m in runner.history if m.role == "assistant" and m.tool_calls)
    assert [tc.id for tc in assistant.tool_calls] == ["p.0", "p.1"]


async def test_tool_output_redacts_known_secret(ctx, fake_provider):
    """工具输出里出现的已知 provider key 明文，入 history 前被脱敏（兜底层）。"""
    from sophagent.config import ProviderConfig, get_config
    secret = "VaDnSECRET86charsSophnetKeyValue1234567890"
    get_config().providers["p"] = ProviderConfig(name="p", api_mode="openai", api_key=secret)
    fake_provider([
        AssistantTurn(tool_calls=[ToolCall(id="c1", name="terminal",
                                           arguments={"command": f"echo {secret}"})]),
        AssistantTurn(content="done", stop_reason="stop"),
    ])
    runner = AgentRunner(ctx.agent, ctx, history=[])
    events = await collect(runner, "leak it")
    tool_msgs = [m for m in runner.history if m.role == "tool"]
    assert tool_msgs and secret not in tool_msgs[0].content
    assert "[REDACTED]" in tool_msgs[0].content
    # 广播给 UI 的 tool_result 预览也不含明文
    preview = next(e for e in events if e["type"] == "tool_result")["preview"]
    assert secret not in preview
