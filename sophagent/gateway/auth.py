"""WebSocket authentication & request validation.

Browsers cannot set custom headers on a WebSocket handshake, so the JWT rides
in a query parameter (``?token=<jwt>``). We validate it before accepting the
connection — a failure closes the socket with code 1008 (policy violation)
rather than exposing a half-authenticated channel.

Origin/Host is checked against the bound host to deflect DNS-rebinding (a
browser that resolves a hostile hostname to localhost can otherwise open a WS
to a server bound there).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import jwt
from starlette.websockets import WebSocket

from .. import auth as auth_mod
from ..config import get_config

log = logging.getLogger(__name__)


async def authenticate(ws: WebSocket) -> Optional[dict[str, Any]]:
    """Resolve the JWT from the ``token`` query param to a live user row.

    Returns the user dict on success, or ``None`` (after closing with 1008)
    on any auth failure. Caller must abort on ``None``.
    """
    token = ws.query_params.get("token")
    if not token:
        await _reject(ws, "missing token")
        return None
    try:
        payload = auth_mod.decode_token(token)
    except jwt.PyJWTError:
        await _reject(ws, "invalid or expired token")
        return None

    user = await ws.app.state.db.get_user(int(payload["sub"]))
    if user is None:
        await _reject(ws, "user no longer exists")
        return None
    return user


def check_origin(ws: WebSocket) -> bool:
    """Reject cross-host handshakes (DNS-rebinding defence).

    We compare the request ``Host`` header against any of the hosts the server
    is configured to serve. When the config lists no explicit allowed hosts we
    accept any host (development / tests)."""
    allowed = get_config().allowed_hosts
    if not allowed:
        return True
    host = (ws.headers.get("host") or "").lower()
    # strip the port for matching; allowed entries may be host or host:port
    host_no_port = host.split(":", 1)[0]
    for entry in allowed:
        e = entry.lower()
        if host == e or host_no_port == e.split(":", 1)[0]:
            return True
    return False


async def _reject(ws: WebSocket, reason: str) -> None:
    log.warning("ws auth rejected: %s peer=%s", reason, _peer(ws))
    try:
        await ws.close(code=1008, reason=reason)
    except Exception:
        pass


def _peer(ws: WebSocket) -> str:
    client = getattr(ws, "client", None)
    if client is None:
        return "unknown"
    return f"{client.host}:{client.port}" if client.port else str(client.host)
