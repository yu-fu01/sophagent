"""WebSocket connection handler for the JSON-RPC gateway.

One ``handle_ws`` coroutine per connection. It authenticates the JWT from the
query string, sends ``gateway.ready``, then pumps inbound JSON-RPC requests
through :func:`sophagent.gateway.dispatcher.dispatch` and writes each response.

On disconnect it hands every session this connection owned to the detach grace
window: the running turn keeps going, events keep buffering, and a reconnect
inside the grace window replays what was missed. After the window, the session
is reaped.
"""

from __future__ import annotations

import logging

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..config import get_config
from . import auth as gateway_auth
from . import dispatcher, protocol
from .methods import GatewayContext
from .session_state import SessionRegistry
from .transport import WSTransport, ws_peer_label

log = logging.getLogger(__name__)


async def handle_ws(ws: WebSocket) -> None:
    peer = ws_peer_label(ws)

    # ── auth (before accept) ───────────────────────────────────────────────
    user = await gateway_auth.authenticate(ws)
    if user is None:
        return  # authenticate already closed with 1008
    if not gateway_auth.check_origin(ws):
        log.warning("ws origin/host rejected peer=%s", peer)
        try:
            await ws.close(code=1008, reason="host not allowed")
        except Exception:
            pass
        return

    await ws.accept()
    log.info("ws accepted peer=%s user=%s", peer, user["id"])

    registry: SessionRegistry = ws.app.state.gateway_registry
    transport = WSTransport(ws, peer=peer)

    # gateway.ready — tells the client it may now resume a session / send.
    if not await transport.emit(protocol.make_event("gateway.ready", None, {"server": "sophagent"})):
        log.warning("ws ready frame send failed peer=%s", peer)
        await _safe_close(ws)
        return

    ctx = GatewayContext(
        db=ws.app.state.db,
        manager=ws.app.state.manager,
        skill_store=ws.app.state.skill_store,
        user=user,
        registry=registry,
        transport=transport,
    )

    try:
        while True:
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                break
            line = raw.strip()
            if not line:
                continue
            try:
                req = protocol.parse_request(line)
            except ValueError:  # json.JSONDecodeError
                await transport.emit(protocol.make_error(None, protocol.ERR_PARSE_ERROR))
                continue
            resp = await dispatcher.dispatch(req, ctx)
            if resp is not None and not await transport.emit(resp):
                log.warning("ws response send failed peer=%s", peer)
                break
    except Exception:
        log.exception("ws loop crashed peer=%s", peer)
    finally:
        transport.close()
        # detach every session this connection owned (turns keep running in bg)
        detached = registry.detach_owned_by(transport)
        await _safe_close(ws)
        log.info("ws closed peer=%s detached=%d", peer, detached)


async def _safe_close(ws: WebSocket) -> None:
    try:
        await ws.close()
    except Exception:
        pass


def build_registry() -> SessionRegistry:
    cfg = get_config()
    return SessionRegistry(grace_seconds=cfg.ws_grace_seconds)
