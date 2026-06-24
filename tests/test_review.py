"""后台自改进 review（子项目③）核心测试：run_review 在受限工具下重放对话、
写记忆/改 skill、不污染 session、无 memory/skill 工具时短路。脚本化 fake provider。"""

from __future__ import annotations

import pytest

from sophclaw import config as config_mod
from sophclaw import providers as providers_mod
from sophclaw.config import ProviderConfig
from sophclaw.db import Database
from sophclaw.models import AgentDef, AssistantTurn, Message, StreamEvent, ToolCall


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
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    providers_mod.reset_providers()
    from sophclaw.tools import load_all
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
    from sophclaw.agent.review import run_review

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
async def test_review_nothing_to_save(tmp_path, monkeypatch):
    from sophclaw.agent.review import run_review

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
    from sophclaw.agent.review import run_review

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
    from sophclaw.agent.review import run_review

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
    from sophclaw.agent.review import _review_tools

    agent = AgentDef(id=1, name="a", description="", system_prompt="p",
                     provider="test", model="m",
                     tools=["memory", "skill_manage", "terminal", "read_file"], skills=None)
    allowed = set(_review_tools(agent))
    assert "memory" in allowed and "skill_manage" in allowed
    assert "terminal" not in allowed and "read_file" not in allowed
