import os

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
