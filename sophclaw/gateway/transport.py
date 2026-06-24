"""Transport abstraction for the WebSocket gateway.

sophclaw is single-process asyncio (unlike hermes, which marshals writes across
threads), so this is a thin, fully-async layer: every transport is just an
async ``emit(frame)`` plus a ``close()``.

Three concrete transports:

* :class:`WSTransport` — owns a live WebSocket; ``emit`` sends immediately.
* :class:`DetachedTransport` — used while no client is connected: writes go
  nowhere (the running turn still buffers them in :class:`SessionState`, which
  is what powers reconnect replay), but the call never raises.
* :class:`NullTransport` — placeholder before a session is resumed.

The active transport for the *current* asyncio task is tracked in a
:mod:`contextvars.ContextVar` so method handlers — and anything they spawn
into a task via :func:`copy_context` — route emits to the right session.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Optional, Protocol, runtime_checkable

from . import protocol

log = logging.getLogger(__name__)


@runtime_checkable
class Transport(Protocol):
    """Anything that can accept one JSON-RPC frame and forward it to a peer."""

    async def emit(self, frame: dict[str, Any]) -> bool:
        """Send one frame. Returns True on success, False if the peer is gone."""
        ...

    def close(self) -> None: ...


# ContextVar: the transport bound for the currently-running request/task. A
# slow method spawns its agent task with ``contextvars.copy_context()`` so the
# runner's emits land on the owning session's transport even after the
# dispatcher returns.
_current_transport: contextvars.ContextVar[Optional[Transport]] = contextvars.ContextVar(
    "sophclaw_gateway_transport", default=None
)


def current_transport() -> Optional[Transport]:
    return _current_transport.get()


def bind_transport(transport: Optional[Transport]):
    """Bind *transport* for the current context. Returns a reset token."""
    return _current_transport.set(transport)


def reset_transport(token) -> None:
    _current_transport.reset(token)


class WSTransport:
    """Per-connection live WebSocket transport."""

    def __init__(self, ws: Any, *, peer: str = "unknown") -> None:
        self._ws = ws
        self._peer = peer
        self._closed = False

    async def emit(self, frame: dict[str, Any]) -> bool:
        if self._closed:
            return False
        try:
            await self._ws.send_text(protocol.encode(frame))
            return True
        except Exception as exc:  # socket gone mid-send
            self._closed = True
            log.warning("ws emit failed peer=%s error=%s", self._peer, exc)
            return False

    def close(self) -> None:
        self._closed = True

    @property
    def peer(self) -> str:
        return self._peer


class DetachedTransport:
    """No client connected. Drops writes silently — they are still buffered by
    :class:`SessionState`, which is what reconnect replay reads from."""

    async def emit(self, frame: dict[str, Any]) -> bool:
        return True

    def close(self) -> None:
        pass


class NullTransport(DetachedTransport):
    """Default transport before any client has resumed a session."""

    pass


def ws_peer_label(ws: Any) -> str:
    client = getattr(ws, "client", None)
    if client is None:
        return "unknown"
    host = getattr(client, "host", None) or "unknown"
    port = getattr(client, "port", None)
    return f"{host}:{port}" if port is not None else host
