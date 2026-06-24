"""/model — switch model for the current session."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def handle(args: str, ctx: dict) -> dict:
    """Set override_model on the session."""
    db = ctx["db"]
    session = ctx["session"]
    user = ctx["user"]

    if not session:
        return {"content": "No active session. Create one first."}

    if not args:
        info = await _model_info(session, {"db": db, "user": user})
        lines = [
            "**当前会话模型：**",
            f"- Provider: `{info['provider'] or '-'}`",
            f"- Agent 默认模型: `{info['agent_model'] or '-'}`",
            f"- 当前覆盖模型: `{info['override_model'] or '(未设置，使用 agent 默认模型)'}`",
            "",
            "**可用模型：**",
        ]
        if info["models"]:
            lines.extend(f"  • `{m}`" for m in info["models"])
        else:
            lines.append("  （没有获取到模型列表）")
        if info["error"]:
            lines.extend(["", f"获取模型列表失败：{info['error']}"])
        lines.extend(["", "使用方式：`/model <模型名>`"])
        return {"content": "\n".join(lines)}

    model_name = args.strip()
    session_id = session["id"]

    await db.set_session_overrides(session_id, override_model=model_name)
    log.info("session %s model overridden to %s by user %d", session_id, model_name, user["id"])

    return {"content": f"✅ Model switched to **{model_name}** for this session."}


async def suggest_models(session: dict[str, Any] | None, ctx: dict[str, Any]) -> list[dict]:
    """Dynamic suggestion resolver for /model.

    Returns model names from the current session's provider.
    Falls back gracefully if no session or provider info is available.
    """
    from ..registry import Suggestion

    if not session:
        return [Suggestion(label="<create a session first>", description="No active session")]

    info = await _model_info(session, ctx)
    if info["models"]:
        return [Suggestion(label=m, description=f"{info['provider']} 模型") for m in info["models"]]
    if info["agent_model"]:
        return [Suggestion(label=info["agent_model"], description="当前 agent 默认模型")]
    if info["error"]:
        return [Suggestion(label="<failed to fetch models>", description=info["error"][:60])]
    return [Suggestion(label="<no models available>", description="当前 provider 没有返回模型")]


async def _model_info(session: dict[str, Any] | None, ctx: dict[str, Any]) -> dict[str, Any]:
    """Resolve provider/model context for /model output and suggestions."""
    from ....providers.registry import get_registry

    if not session:
        return {
            "provider": None,
            "agent_model": None,
            "override_model": None,
            "models": [],
            "error": "No active session",
        }
    if not isinstance(session, dict):
        session = dict(session)

    db = ctx.get("db")
    provider_name = session.get("override_provider")
    agent_model = None
    agent_id = session.get("agent_id")
    if db and agent_id:
        agent = await db.get_agent(agent_id)
        if agent:
            provider_name = provider_name or (agent.get("provider") if isinstance(agent, dict) else agent["provider"])
            agent_model = agent.get("model") if isinstance(agent, dict) else agent["model"]

    if not provider_name:
        return {
            "provider": None,
            "agent_model": agent_model,
            "override_model": session.get("override_model"),
            "models": [],
            "error": "Session has no provider",
        }

    reg = get_registry()
    if provider_name not in reg.names():
        return {
            "provider": provider_name,
            "agent_model": agent_model,
            "override_model": session.get("override_model"),
            "models": [agent_model] if agent_model else [],
            "error": f"Unknown provider: {provider_name}",
        }

    try:
        models = await reg.list_models(provider_name)
    except Exception as e:
        log.warning("Failed to list models for provider %s: %s", provider_name, e)
        models = [agent_model] if agent_model else []
        error = str(e)
    else:
        error = ""

    return {
        "provider": provider_name,
        "agent_model": agent_model,
        "override_model": session.get("override_model"),
        "models": models,
        "error": error,
    }