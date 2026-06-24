"""JSON-RPC method handlers for the WebSocket gateway.

Each handler is ``async def(params: dict, ctx: GatewayContext) -> dict`` and
returns a plain result dict; the dispatcher wraps it in a JSON-RPC response.
Slow methods (``prompt.submit`` etc.) start a background task that streams
events onto the session's transport, then return an immediate ack.

The turn itself runs through the shared :mod:`sophagent.agent.turn` core —
identical to the REST SSE path — so behaviour stays in lock-step between
transports.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ..agent.turn import pre_submit, run_turns
from ..config import get_config
from ..gateway import protocol
from ..gateway.session_state import SessionRegistry, SessionState
from ..gateway.transport import Transport

log = logging.getLogger(__name__)


class GatewayError(Exception):
    """A method-level error mapped to a JSON-RPC error response."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class GatewayContext:
    """Per-connection context handed to every method handler."""

    db: Any
    manager: Any
    skill_store: Any
    user: dict[str, Any]
    registry: SessionRegistry
    transport: Transport   # this connection's transport


async def _require_session(ctx: GatewayContext, session_id: str) -> SessionState:
    """Resolve a session the caller owns, returning its run-state.

    Raises GatewayError(4401) if the session doesn't exist or isn't the
    caller's — same ownership rule as the REST chat route (creator-private)."""
    session = await ctx.db.get_session(session_id, ctx.user["id"])
    if session is None:
        raise GatewayError(protocol.ERR_FORBIDDEN, "session not owned by caller")
    return ctx.registry.get_or_create(session_id, ctx.user["id"])


async def _start_turn(ctx: GatewayContext, state: SessionState, content: str | None) -> dict[str, Any]:
    """Start a turn on ``state`` streaming events onto its transport. When the
    turn settles, a decoupled background self-improvement review is scheduled."""
    async def _on_done() -> None:
        schedule_review(ctx, state)

    task = state.start_turn(functools.partial(
        run_turns,
        session_id=state.session_id,
        user_input=content,
        db=ctx.db,
        manager=ctx.manager,
        skill_store=ctx.skill_store,
        user_id=ctx.user["id"],
    ), on_done=_on_done)
    ctx.manager.register_task(state.session_id, task)
    return {"started": True, "session_id": state.session_id}


async def _review_and_notify(ctx: GatewayContext, state: SessionState) -> None:
    """Run the background review for the session's latest history and, if it
    saved anything, push an out-of-turn notice. Never raises; always clears the
    in-flight flag."""
    from ..agent.review import run_review
    from ..models import AgentDef

    try:
        session = await ctx.db.get_session(state.session_id, state.user_id)
        if session is None:
            return
        agent_row = await ctx.db.get_agent(session["agent_id"])
        if agent_row is None:
            return
        history = await ctx.db.load_messages(state.session_id)
        result = await run_review(
            db=ctx.db, skill_store=ctx.skill_store,
            agent=AgentDef.from_row(agent_row), user_id=state.user_id, history=history,
        )
        if result.changed:
            await state.push_review_notice(result.summary, result.actions)
    except Exception:
        log.exception("background review failed session=%s", state.session_id)
    finally:
        state.review_running = False


def schedule_review(ctx: GatewayContext, state: SessionState) -> Optional[asyncio.Task]:
    """Spawn a decoupled background review task, or return None when it's off /
    already running. Sets ``review_running`` so rapid turns don't stack reviews."""
    if not get_config().self_improve_enabled:
        return None
    if state.review_running:
        return None
    state.review_running = True
    return asyncio.create_task(_review_and_notify(ctx, state))


# ── methods ────────────────────────────────────────────────────────────────

async def m_session_resume(params: dict[str, Any], ctx: GatewayContext) -> dict[str, Any]:
    """Take over a session: bind this transport and replay the current
    in-flight turn's buffered events (empty if no turn is running)."""
    session_id = params["session_id"]
    state = await _require_session(ctx, session_id)
    replay = state.resume(ctx.transport)
    # flush buffered frames to the (re)connected client, oldest-first
    for frame in replay:
        await ctx.transport.emit(frame)
    return {
        "session_id": session_id,
        "history_version": state.history_version,
        "running": state.running,
        "replayed": len(replay),
    }


async def m_prompt_submit(params: dict[str, Any], ctx: GatewayContext) -> dict[str, Any]:
    """Send a user message, driving a turn (or auto-enqueuing when busy)."""
    session_id = params["session_id"]
    content = params["content"]
    state = await _require_session(ctx, session_id)

    kind, payload = await pre_submit(
        session_id=session_id, content=content, db=ctx.db, manager=ctx.manager,
        skill_store=ctx.skill_store, user=ctx.user,
    )
    if kind == "slash":
        # slash command handled + persisted; surface its result dict
        return {"kind": "slash", "result": payload}
    if kind == "retry":
        await _start_turn(ctx, state, None)
        return {"kind": "retry", "started": True}
    if kind == "queued":
        return {"kind": "queued", "position": payload}
    if kind == "queue_full":
        raise GatewayError(protocol.ERR_BUSY, "消息队列已满（最多 3 条），请等待当前回复完成后再试")
    # kind == "start"
    await _start_turn(ctx, state, payload)
    return {"kind": "start", "started": True}


async def m_session_interrupt(params: dict[str, Any], ctx: GatewayContext) -> dict[str, Any]:
    """Stop the running turn for a session."""
    session_id = params["session_id"]
    state = await _require_session(ctx, session_id)
    stopped = state.interrupt()
    # also drop any queued messages, matching the REST stop route
    ctx.manager.clear_queue(session_id)
    return {"stopped": stopped}


async def m_message_edit(params: dict[str, Any], ctx: GatewayContext) -> dict[str, Any]:
    """Re-edit: drop the message with ``message_id`` and everything after it,
    then run a fresh turn with ``content``. Refused while a turn is running."""
    session_id = params["session_id"]
    message_id = params["message_id"]
    content = params["content"]
    if ctx.manager.is_busy(session_id):
        raise GatewayError(protocol.ERR_BUSY, "session is running a turn")
    state = await _require_session(ctx, session_id)
    deleted = await ctx.db.truncate_from(session_id, message_id)
    await ctx.db.touch_session(session_id)
    await _start_turn(ctx, state, content)
    return {"deleted": deleted, "started": True}


async def m_slash_exec(params: dict[str, Any], ctx: GatewayContext) -> dict[str, Any]:
    """Run a slash command (same dispatch as prompt.submit's slash path)."""
    session_id = params["session_id"]
    command = params["command"]
    state = await _require_session(ctx, session_id)
    kind, payload = await pre_submit(
        session_id=session_id, content=command, db=ctx.db, manager=ctx.manager,
        skill_store=ctx.skill_store, user=ctx.user,
    )
    if kind == "retry":
        await _start_turn(ctx, state, None)
        return {"kind": "retry", "started": True}
    # slash result / queued / start all surface here; for "start" the slash
    # wasn't a command (no leading slash) — treat like prompt.submit start.
    if kind == "start":
        await _start_turn(ctx, state, payload)
        return {"kind": "start", "started": True}
    return {"kind": kind, "result": payload}


async def m_session_truncate(params: dict[str, Any], ctx: GatewayContext) -> dict[str, Any]:
    """Restore: drop the message with ``message_id`` and everything after it.
    No turn is started (unlike ``message.edit``). Refused while a turn runs."""
    session_id = params["session_id"]
    message_id = params["message_id"]
    if ctx.manager.is_busy(session_id):
        raise GatewayError(protocol.ERR_BUSY, "session is running a turn")
    await _require_session(ctx, session_id)   # 归属校验（4401）
    deleted = await ctx.db.truncate_from(session_id, message_id)
    await ctx.db.touch_session(session_id)
    return {"deleted": deleted}


async def m_queue_submit(params: dict[str, Any], ctx: GatewayContext) -> dict[str, Any]:
    """Explicitly enqueue a message (used by the client when it already knows
    the session is busy)."""
    session_id = params["session_id"]
    content = params["content"]
    await _require_session(ctx, session_id)
    if not ctx.manager.try_put(session_id, content):
        raise GatewayError(protocol.ERR_BUSY, "消息队列已满（最多 3 条）")
    await ctx.db.touch_session(session_id)
    return {"queued": True, "position": ctx.manager.pending_count(session_id)}


# method name → handler
METHODS: dict[str, Callable[[dict[str, Any], GatewayContext], Awaitable[dict[str, Any]]]] = {
    "session.resume": m_session_resume,
    "prompt.submit": m_prompt_submit,
    "session.interrupt": m_session_interrupt,
    "message.edit": m_message_edit,
    "session.truncate": m_session_truncate,
    "slash.exec": m_slash_exec,
    "queue.submit": m_queue_submit,
}
