"""Per-session run-time state for the WebSocket gateway.

The detach / buffer / replay / grace / reap lifecycle lives here. The turn
itself (agent loop, persistence, message queue) is shared with the REST SSE
path via :mod:`sophclaw.agent.turn`; this module only owns *how events leave
the running turn*:

* the current turn's events are buffered unconditionally (``deque(maxlen=500)``)
  so a client that disconnects mid-turn and reconnects within the grace window
  sees everything it missed — hermes' original detach dropped intermediate
  tokens and relied on the final DB write; sophclaw replays instead;
* only the *current in-flight turn* is buffered; once a turn is persisted it is
  cleared and the client recovers it from the DB on resume (no replay of past
  turns);
* at most one transport is active per session — a reconnect *takes over*,
  cancelling any pending grace timer.

Lifecycle
---------
turn start → clear buffer (fresh turn) → task drives ``run_turns``, calling
``on_event`` per event (buffer + forward) → on ``done`` → ``finish_turn``
(persisted by the runner; here we bump ``history_version``, emit
``session.info``, clear the buffer).

disconnect → ``detach`` (transport swapped to :class:`DetachedTransport`,
grace timer armed; the task keeps running).

reconnect → ``resume`` (new transport takes over, buffered current-turn events
replayed, grace timer cancelled).

grace expiry → ``reap`` (task cancelled, state dropped; DB history already
persisted, so the conversation is not lost).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from . import protocol
from .transport import DetachedTransport, NullTransport, Transport, WSTransport

log = logging.getLogger(__name__)

EVENT_BUFFER_MAX = 500   # per in-flight turn
DEFAULT_GRACE_SECONDS = 60.0


@dataclass
class SessionState:
    """All mutable run-time state for one session's gateway connection."""

    session_id: str
    user_id: int
    registry: "SessionRegistry"
    history_version: int = 0
    running: bool = False
    task: Optional[asyncio.Task] = None
    transport: Transport = field(default_factory=NullTransport)
    events: deque = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER_MAX))
    grace_handle: Optional[asyncio.TimerHandle] = None
    grace_seconds: float = DEFAULT_GRACE_SECONDS

    # ── event flow ─────────────────────────────────────────────────────────

    async def on_event(self, ev: dict[str, Any]) -> None:
        """One internal agent event → buffer (unconditionally) + forward live."""
        wire_type, payload = protocol.remap_event(ev)
        frame = protocol.make_event(wire_type, self.session_id, payload)
        self.events.append(frame)
        try:
            await self.transport.emit(frame)
        except Exception:
            log.exception("emit to transport failed session=%s", self.session_id)

    async def finish_turn(self) -> None:
        """A turn just completed (already persisted by the runner). Bump the
        history watermark, tell the client, then clear the per-turn buffer so
        the next turn starts fresh and the buffer never replays past turns."""
        self.history_version += 1
        info = protocol.make_event(
            "session.info", self.session_id,
            {"history_version": self.history_version},
        )
        self.events.append(info)
        try:
            await self.transport.emit(info)
        except Exception:
            log.exception("emit session.info failed session=%s", self.session_id)
        self.events.clear()

    # ── turn driving ────────────────────────────────────────────────────────

    def start_turn(
        self,
        turn_factory: Callable[[], AsyncIterator[dict[str, Any]]],
        on_done: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> asyncio.Task:
        """Drive ``turn_factory()`` (an async generator of events) as a task.

        The session's transport is bound to the task's contextvars context so
        anything the turn spawns can reach it via :func:`current_transport`
        (event delivery itself uses ``self.transport`` directly)."""
        self.events.clear()           # fresh buffer for this turn
        self.running = True
        self._cancel_grace()
        self.task = asyncio.create_task(self._runner(turn_factory(), on_done))
        return self.task

    async def _runner(
        self,
        gen: AsyncIterator[dict[str, Any]],
        on_done: Optional[Callable[[], Awaitable[None]]],
    ) -> None:
        from .transport import bind_transport, reset_transport

        token = bind_transport(self.transport)
        try:
            async for ev in gen:
                await self.on_event(ev)
                if ev.get("type") == "done":
                    await self.finish_turn()
            if on_done:
                await on_done()
        except asyncio.CancelledError:
            log.info("turn cancelled session=%s", self.session_id)
            raise
        except Exception:
            log.exception("turn driver crashed session=%s", self.session_id)
            await self.on_event({"type": "error", "message": "internal gateway error"})
        finally:
            self.running = False
            # turn.settled: WS-only end-of-turn-task signal so the client can
            # finalize streaming UI. Not buffered (a reconnect reads `running`
            # from the resume ack instead); sent straight to the live transport.
            await self._emit_control("settled")
            reset_transport(token)

    async def _emit_control(self, internal_type: str) -> None:
        wire_type, payload = protocol.remap_event({"type": internal_type})
        frame = protocol.make_event(wire_type, self.session_id, payload)
        try:
            await self.transport.emit(frame)
        except Exception:
            log.debug("control emit dropped session=%s", self.session_id)

    # ── detach / replay / reap ──────────────────────────────────────────────

    def detach(self) -> None:
        """Client gone. Keep the turn alive, just stop trying to send."""
        self._cancel_grace()
        self.transport = DetachedTransport()
        loop = asyncio.get_running_loop()
        self.grace_handle = loop.call_later(self.grace_seconds, self._grace_expired)

    def resume(self, transport: Transport) -> list[dict[str, Any]]:
        """A client (re)connects and takes over. Replay the current in-flight
        turn's buffered events (empty if no turn is running) and cancel grace.
        Returns the replayed frames so the caller can log/inspect."""
        self._cancel_grace()
        self.transport = transport
        replay = list(self.events)
        return replay

    def _grace_expired(self) -> None:
        if self.grace_handle is None:
            return  # already resumed/reaped
        self.grace_handle = None
        log.info("gateway grace expired, reaping session=%s", self.session_id)
        self.reap()

    def reap(self) -> None:
        """Grace window elapsed with no reconnect: cancel the turn, drop state.
        Persisted history is already in the DB, so the conversation survives."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self._cancel_grace()
        self.running = False
        self.transport = DetachedTransport()
        self.registry.discard(self.session_id)

    def _cancel_grace(self) -> None:
        if self.grace_handle is not None:
            self.grace_handle.cancel()
            self.grace_handle = None

    def interrupt(self) -> bool:
        """Stop the running turn (best effort). Returns whether a task was cancelled."""
        if self.task is not None and not self.task.done():
            self.task.cancel()
            return True
        return False


class SessionRegistry:
    """Process-global map of live session run-state (single-process sophclaw)."""

    def __init__(self, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> None:
        self._states: dict[str, SessionState] = {}
        self.grace_seconds = grace_seconds

    def get_or_create(self, session_id: str, user_id: int) -> SessionState:
        state = self._states.get(session_id)
        if state is None:
            state = SessionState(
                session_id=session_id, user_id=user_id, registry=self,
                grace_seconds=self.grace_seconds,
            )
            self._states[session_id] = state
        return state

    def get(self, session_id: str) -> Optional[SessionState]:
        return self._states.get(session_id)

    def discard(self, session_id: str) -> None:
        self._states.pop(session_id, None)

    def all(self) -> list[SessionState]:
        return list(self._states.values())

    def detach_owned_by(self, transport: Transport) -> int:
        """On a ws disconnect: detach every session whose active transport is
        this connection's transport. Returns the count detached."""
        n = 0
        for state in list(self._states.values()):
            if isinstance(state.transport, WSTransport) and state.transport is transport:
                state.detach()
                n += 1
        return n
