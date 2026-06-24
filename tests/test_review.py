"""后台自改进 review（子项目③）核心测试：run_review 在受限工具下重放对话、
写记忆/改 skill、不污染 session、无 memory/skill 工具时短路。脚本化 fake provider。"""

from __future__ import annotations

import pytest

from sophagent import config as config_mod
from sophagent import providers as providers_mod
from sophagent.config import ProviderConfig
from sophagent.db import Database
from sophagent.models import AgentDef, AssistantTurn, Message, StreamEvent, ToolCall


class ScriptedProvider:
    """Yields pre-scripted AssistantTurns in order (each = one model response)."""

    def __init__(self, script: list[AssistantTurn]):
        self.script = list(script)
        self.calls = 0

    async def chat(self, *, model, system, messages, tools=None,
                   temperature=None, max_tokens=None, thinking=None):
        self.calls += 1
        turn = self.script.pop(0) if self.script else AssistantTurn(
            content="Nothing to save.", stop_reason="stop", input_tokens=1, output_tokens=1)
        if turn.content:
            yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("turn_done", turn=turn)


async def _setup(tmp_path, monkeypatch, script, tools):
    monkeypatch.setenv("SOPHAGENT_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    providers_mod.reset_providers()
    from sophagent.tools import load_all
    load_all()  # populate the tool registry (the server does this at startup)
    cfg = config_mod.get_config()
    cfg.providers["test"] = ProviderConfig(name="test", api_mode="openai", context_limit=100_000)
    provider = ScriptedProvider(script)
    providers_mod._cache["test"] = provider

    db = Database(tmp_path / "r.db")
    await db.connect()
    await db._exec(
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (1,'u','h','t')"
    )
    await db.conn.commit()
    agent = AgentDef(
        id=1, name="a", description="", system_prompt="p",
        provider="test", model="test-model", tools=tools, skills=None,
    )
    return db, agent, provider


# -- ③ 单元 3：run_review --------------------------------------------------


@pytest.mark.asyncio
async def test_review_saves_memory_returns_changed(tmp_path, monkeypatch):
    from sophagent.agent.review import run_review

    # 第一轮：review agent 调 memory 写一条用户画像；第二轮：收尾无工具
    script = [
        AssistantTurn(tool_calls=[ToolCall("c1", "memory", {
            "action": "add", "content": "User prefers terse replies", "target": "user"})],
            stop_reason="tool_calls", input_tokens=5, output_tokens=5),
        AssistantTurn(content="Saved.", stop_reason="stop", input_tokens=1, output_tokens=1),
    ]
    db, agent, provider = await _setup(tmp_path, monkeypatch, script, ["memory"])
    try:
        result = await run_review(db=db, skill_store=None, agent=agent, user_id=1, history=[
            Message(role="user", content="be terse please"),
            Message(role="assistant", content="ok"),
        ])
        assert result.changed is True
        rows = await db.memory_list(1, target="user")
        assert [r["content"] for r in rows] == ["User prefers terse replies"]
    finally:
        await db.close()
        config_mod.reset_config()
        providers_mod.reset_providers()


@pytest.mark.asyncio
async def test_review_read_only_is_not_a_change(tmp_path, monkeypatch):
    """review 只是 read 记忆查看现状、未写入 → changed=False（不能把 read 当变更）。"""
    from sophagent.agent.review import run_review

    script = [
        AssistantTurn(tool_calls=[ToolCall("c1", "memory", {
            "action": "read", "target": "user"})],
            stop_reason="tool_calls", input_tokens=5, output_tokens=5),
        AssistantTurn(content="Nothing to save.", stop_reason="stop", input_tokens=1, output_tokens=1),
    ]
    db, agent, provider = await _setup(tmp_path, monkeypatch, script, ["memory"])
    await db.memory_add(1, "existing user fact", target="user")
    try:
        result = await run_review(db=db, skill_store=None, agent=agent, user_id=1,
                                  history=[Message(role="user", content="hi")])
        assert result.changed is False  # 只读不算变更
    finally:
        await db.close()
        config_mod.reset_config()
        providers_mod.reset_providers()


@pytest.mark.asyncio
async def test_review_nothing_to_save(tmp_path, monkeypatch):
    from sophagent.agent.review import run_review

    script = [AssistantTurn(content="Nothing to save.", stop_reason="stop",
                            input_tokens=1, output_tokens=1)]
    db, agent, provider = await _setup(tmp_path, monkeypatch, script, ["memory"])
    try:
        result = await run_review(db=db, skill_store=None, agent=agent, user_id=1,
                                  history=[Message(role="user", content="hi")])
        assert result.changed is False
        assert await db.memory_list(1) == []
    finally:
        await db.close()
        config_mod.reset_config()
        providers_mod.reset_providers()


@pytest.mark.asyncio
async def test_review_short_circuits_without_memory_or_skill_tools(tmp_path, monkeypatch):
    from sophagent.agent.review import run_review

    db, agent, provider = await _setup(tmp_path, monkeypatch, [], ["read_file", "terminal"])
    try:
        result = await run_review(db=db, skill_store=None, agent=agent, user_id=1,
                                  history=[Message(role="user", content="hi")])
        assert result.changed is False
        assert provider.calls == 0  # 没调用模型
    finally:
        await db.close()
        config_mod.reset_config()
        providers_mod.reset_providers()


@pytest.mark.asyncio
async def test_review_does_not_persist_session_messages(tmp_path, monkeypatch):
    """review 重放不应通过 on_persist 写任何 session 消息（on_persist=None）。"""
    from sophagent.agent.review import run_review

    script = [AssistantTurn(content="Nothing to save.", stop_reason="stop",
                            input_tokens=1, output_tokens=1)]
    db, agent, provider = await _setup(tmp_path, monkeypatch, script, ["memory"])
    try:
        # run_review 不接受 on_persist 参数；这里确认它内部不碰 messages 表
        before = await db._all("SELECT count(*) AS c FROM messages")
        await run_review(db=db, skill_store=None, agent=agent, user_id=1,
                         history=[Message(role="user", content="hi")])
        after = await db._all("SELECT count(*) AS c FROM messages")
        assert before[0]["c"] == after[0]["c"] == 0
    finally:
        await db.close()
        config_mod.reset_config()
        providers_mod.reset_providers()


def test_review_tool_whitelist():
    """_review_tools 收窄到 memory/skill 工具与 agent 已开工具的交集。"""
    from sophagent.agent.review import _review_tools

    agent = AgentDef(id=1, name="a", description="", system_prompt="p",
                     provider="test", model="m",
                     tools=["memory", "skill_manage", "terminal", "read_file"], skills=None)
    allowed = set(_review_tools(agent))
    assert "memory" in allowed and "skill_manage" in allowed
    assert "terminal" not in allowed and "read_file" not in allowed


# -- ③ 单元 4：网关调度 + out-of-turn 通知 ---------------------------------


class CaptureTransport:
    def __init__(self):
        self.frames = []

    async def emit(self, frame):
        self.frames.append(frame)
        return True

    def close(self):
        pass


def _state(session_id="s1", user_id=1, transport=None):
    from sophagent.gateway.session_state import SessionRegistry, SessionState

    reg = SessionRegistry()
    st = SessionState(session_id=session_id, user_id=user_id, registry=reg)
    if transport is not None:
        st.transport = transport
    return st


def _gctx(db, transport, user_id=1):
    from sophagent.gateway.methods import GatewayContext
    from sophagent.gateway.session_state import SessionRegistry

    return GatewayContext(
        db=db, manager=None, skill_store=None,
        user={"id": user_id}, registry=SessionRegistry(), transport=transport,
    )


@pytest.mark.asyncio
async def test_push_review_notice_emits_review_event(tmp_path, monkeypatch):
    cap = CaptureTransport()
    st = _state(transport=cap)
    await st.push_review_notice("💾 Saved user memory [1]", ["Saved user memory [1]"])
    assert len(cap.frames) == 1
    assert cap.frames[0]["params"]["type"] == "memory.review"
    assert "Saved" in cap.frames[0]["params"]["payload"]["summary"]


@pytest.mark.asyncio
async def test_push_review_notice_silent_when_detached(tmp_path, monkeypatch):
    from sophagent.gateway.transport import DetachedTransport

    st = _state(transport=DetachedTransport())
    # 不应抛异常
    await st.push_review_notice("x", ["x"])


@pytest.mark.asyncio
async def test_schedule_review_skipped_when_disabled(tmp_path, monkeypatch):
    from sophagent.gateway.methods import schedule_review

    db, agent, provider = await _setup(tmp_path, monkeypatch, [], ["memory"])
    config_mod.get_config().self_improve_enabled = False
    try:
        st = _state(transport=CaptureTransport())
        task = schedule_review(_gctx(db, st.transport), st)
        assert task is None
        assert st.review_running is False
    finally:
        await db.close()
        config_mod.reset_config()
        providers_mod.reset_providers()


@pytest.mark.asyncio
async def test_schedule_review_guarded_against_double_spawn(tmp_path, monkeypatch):
    from sophagent.gateway.methods import schedule_review

    db, agent, provider = await _setup(tmp_path, monkeypatch, [], ["memory"])
    try:
        st = _state(transport=CaptureTransport())
        st.review_running = True  # 已有 review 在飞
        task = schedule_review(_gctx(db, st.transport), st)
        assert task is None
    finally:
        await db.close()
        config_mod.reset_config()
        providers_mod.reset_providers()


@pytest.mark.asyncio
async def test_schedule_review_happy_path_writes_and_notifies(tmp_path, monkeypatch):
    from sophagent.gateway.methods import schedule_review

    script = [
        AssistantTurn(tool_calls=[ToolCall("c1", "memory", {
            "action": "add", "content": "User likes dark mode", "target": "user"})],
            stop_reason="tool_calls", input_tokens=5, output_tokens=5),
        AssistantTurn(content="Saved.", stop_reason="stop", input_tokens=1, output_tokens=1),
    ]
    db, agent, provider = await _setup(tmp_path, monkeypatch, script, ["memory"])
    # 建 agent + session + 历史消息
    cur = await db._exec(
        "INSERT INTO agents (name, description, system_prompt, provider, model, "
        "tools, created_at, updated_at) VALUES ('a','','p','test','test-model','[\"memory\"]','t','t')"
    )
    aid = cur.lastrowid
    await db._exec(
        "INSERT INTO sessions (id, user_id, agent_id, title, created_at, updated_at) "
        "VALUES ('s1',1,?,'','t','t')", (aid,))
    await db.conn.commit()
    await db.append_messages("s1", [Message(role="user", content="i prefer dark mode")])
    try:
        cap = CaptureTransport()
        st = _state(transport=cap)
        task = schedule_review(_gctx(db, cap), st)
        assert task is not None
        await task  # 等后台 review 跑完
        # 记忆已落库
        assert [r["content"] for r in await db.memory_list(1, target="user")] == ["User likes dark mode"]
        # 通知已推送
        assert any(f["params"]["type"] == "memory.review" for f in cap.frames)
        assert st.review_running is False  # finally 清标志
    finally:
        await db.close()
        config_mod.reset_config()
        providers_mod.reset_providers()
