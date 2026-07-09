"""Shared chat-turn core — the single source of truth for how one (or several
chained) conversation turns run and what UI events they emit.

Both transports consume this:

* the REST SSE route (``api/session_routes.py``) pumps yielded events into an
  :class:`asyncio.Queue` that a ``StreamingResponse`` drains;
* the WebSocket ``prompt.submit`` method pumps them through a session's
  :class:`~sophagent.gateway.session_state.SessionState` (buffer + transport).

The agent loop, two-layer context compression, persistence, message queue and
queued-next chaining all live here so both transports stay in lock-step.

The slash / queue *pre-dispatch* (run a slash command, or enqueue while busy,
or start a turn) is factored into :func:`pre_submit` so each transport only
differs in how it reacts to the three outcomes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from ..models import AgentDef, Message
from ..models import MessageAttachment
from .manager import SessionManager
from .runtime import build_runner

log = logging.getLogger(__name__)

# commands that empty the per-session message queue when invoked
_QUEUE_CLEARING_COMMANDS = ("undo", "retry", "clear", "new")


async def pre_submit(
    *,
    session_id: str,
    content: str,
    db,
    manager: SessionManager,
    skill_store,
    user: dict[str, Any],
) -> tuple[str, Any]:
    """Decide what to do with a user message before running a turn.

    Returns one of:

    * ``("slash", result_dict)`` — a slash command was handled (and persisted);
      the caller emits ``result_dict`` and stops.
    * ``("retry", None)`` — the ``/retry`` command truncated history; the
      caller should start a turn with **no** new user input.
    * ``("queued", position)`` — the model is busy; the message was enqueued.
    * ``("queue_full", None)`` — busy and the queue is already at capacity.
    * ``("start", content)`` — start a turn with ``content`` as the user input.
    """
    if content.startswith("/"):
        from .commands import dispatch_command

        session = await db.get_session(session_id, user["id"])
        if session is None:
            return ("slash", {"handled": True, "type": "error", "content": "session not found"})
        result = await dispatch_command(
            content, db, session, user, manager, skill_store=skill_store,
        )
        if result.get("handled"):
            cmd_name = content.lstrip("/").split(maxsplit=1)[0].lower()
            if cmd_name in _QUEUE_CLEARING_COMMANDS:
                manager.clear_queue(session_id)
            if result.get("action") == "retry_stream":
                await db.touch_session(session_id)
                return ("retry", None)
            # persist the user command + the assistant reply so openSession sees them
            await db.append_messages(session_id, [
                Message(role="user", content=content),
                Message(role="assistant", content=result.get("content", "")),
            ])
            await db.touch_session(session_id)
            return ("slash", result)

    if manager.is_busy(session_id):
        if not manager.try_put(session_id, content):
            return ("queue_full", None)
        await db.touch_session(session_id)
        return ("queued", manager.pending_count(session_id))

    return ("start", content)


async def run_turns(
    *,
    session_id: str,
    user_input: str | None,
    db,
    manager: SessionManager,
    skill_store,
    user_id: int,
    user_attachments: list[MessageAttachment] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield UI events for one turn and any queued follow-up turns.

    Acquires the per-session lock (serialises turns), runs under the global
    turn semaphore, persists each message, drains the message queue, and emits
    ``queued_next`` between chained turns. Yields a ``done`` event at the end
    of every turn.
    """
    next_input = user_input
    first_turn = True
    async with manager.lock_for(session_id):
        while True:
            holder: dict[str, Any] = {}
            async for ev in _run_one_turn(
                session_id=session_id, input_text=next_input,
                db=db, manager=manager, skill_store=skill_store,
                user_id=user_id, holder=holder,
                user_attachments=user_attachments if first_turn else None,
            ):
                yield ev
            first_turn = False
            if not holder.get("has_more"):
                return
            next_input = holder.get("next_input")
            if next_input is None:
                return


async def _run_one_turn(
    *,
    session_id: str,
    input_text: str | None,
    db,
    manager: SessionManager,
    skill_store,
    user_id: int,
    holder: dict[str, Any],
    user_attachments: list[MessageAttachment] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run exactly one turn, yielding events. Sets ``holder["has_more"]`` and
    ``holder["next_input"]`` when a queued message follows this one."""
    session = await db.get_session(session_id, user_id)
    if session is None:
        yield {"type": "error", "message": "session was deleted"}
        holder["has_more"] = False
        return

    try:
        async with manager.semaphore:
            history = await db.load_messages(session_id)

            async def persist(msgs):
                await db.append_messages(session_id, msgs)

            agent_row = await db.get_agent(session["agent_id"])
            if agent_row is None:
                yield {"type": "error", "message": "agent definition was deleted"}
                holder["has_more"] = False
                return
            agent = AgentDef.from_row(agent_row)

            runner = await build_runner(
                db=db, skill_store=skill_store, agent=agent,
                user_id=user_id, history=history, on_persist=persist,
                override_provider=session["override_provider"],
                override_model=session["override_model"],
                thinking_mode=session["thinking_mode"],
                session_id=session_id,
            )
            try:
                async for ev in runner.run(input_text, attachments=user_attachments):
                    yield ev
            except asyncio.CancelledError:
                yield {"type": "error", "message": "stopped by user"}
                raise
            finally:
                if runner.compressed:
                    await db.compact_session(session_id, runner.history)
                title = None if session["title"] or input_text is None else input_text[:60]
                await db.touch_session(session_id, title)
    except asyncio.CancelledError:
        holder["has_more"] = False
        raise
    except Exception as e:
        log.exception("chat worker failed")
        yield {"type": "error", "message": str(e)}
        holder["has_more"] = False
        return

    # drain the next queued message (peek without consuming; consumed in the loop)
    next_msg = manager.drain_queue(session_id)
    if next_msg is not None:
        holder["next_input"] = next_msg
        holder["has_more"] = True
        yield {"type": "queued_next", "content": next_msg}
    else:
        holder["has_more"] = False
