import json
import os
from typing import AsyncIterator

import pytest

from sophclaw import config as config_mod


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """A ToolContext with an isolated workspace and data dir."""
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    from sophclaw.models import AgentDef
    from sophclaw.tools.registry import ToolContext, all_tool_names

    agent = AgentDef(
        id=1, name="t", description="", system_prompt="You are a test agent.",
        provider="test", model="test-model", tools=all_tool_names(), skills=None,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    yield ToolContext(user_id=1, workspace=workspace, agent=agent)
    config_mod.reset_config()


# ---------------------------------------------------------------------------
# Shared API test harness (TestClient + scripted fake provider).
# ---------------------------------------------------------------------------


class EchoProvider:
    """Echoes the last user message; runs scripted turns first if provided."""

    def __init__(self):
        from sophclaw.models import AssistantTurn

        self._AssistantTurn = AssistantTurn
        self.script: list = []

    async def chat(self, *, model, system, messages, tools=None,
                   temperature=None, max_tokens=None) -> AsyncIterator:
        from sophclaw.models import StreamEvent

        if self.script:
            turn = self.script.pop(0)
        else:
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            turn = self._AssistantTurn(content=f"echo: {last_user}", stop_reason="stop",
                                       input_tokens=10, output_tokens=5)
        if turn.content:
            yield StreamEvent("text_delta", text=turn.content)
        yield StreamEvent("turn_done", turn=turn)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import sophclaw.providers as providers_mod
    from sophclaw.config import ProviderConfig

    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpw")
    config_mod.reset_config()
    providers_mod.reset_providers()
    cfg = config_mod.get_config()
    cfg.providers["test"] = ProviderConfig(name="test", api_mode="openai", context_limit=100_000)
    provider = EchoProvider()
    providers_mod._cache["test"] = provider

    from sophclaw.main import create_app

    app = create_app()
    with TestClient(app) as c:
        c.provider = provider
        yield c
    config_mod.reset_config()
    providers_mod.reset_providers()


def login(client, username, password) -> dict:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


@pytest.fixture
def admin(client):
    return login(client, "admin", "adminpw")


def make_user(client, admin, username, password) -> dict:
    """Create a normal user via the admin API and return their auth header."""
    resp = client.post("/api/users", json={"username": username, "password": password}, headers=admin)
    assert resp.status_code == 201, resp.text
    return login(client, username, password)


@pytest.fixture
def bob(client, admin):
    return make_user(client, admin, "bob", "bobpw123")


@pytest.fixture
def agent_id(client, bob) -> int:
    # bob owns his personal group, so he may create an agent in it and use it
    resp = client.post("/api/agents", json={
        "name": "helper", "system_prompt": "You are helper.",
        "provider": "test", "model": "test-model",
        "tools": ["read_file", "write_file", "skills_list", "skill_view", "skill_manage", "memory"],
    }, headers=bob)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def uid(client, admin, username) -> int:
    """Look up a user's id by username via the admin API."""
    return next(u["id"] for u in client.get("/api/users", headers=admin).json() if u["username"] == username)


def sse_events(resp) -> list[dict]:
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]
