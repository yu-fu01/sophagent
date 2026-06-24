"""Route inbound JSON-RPC requests to method handlers.

``dispatch(req, ctx)`` is the single entry point the WebSocket loop calls. It
validates the envelope, looks up the handler, runs it, and returns a
JSON-RPC response frame (or ``None`` for notifications with no id).

Notifications (events from the client, no ``id``) are not used by the gateway
today — every inbound message is a request — but we tolerate them by returning
None so a future client-side keepalive/ack cannot wedge the loop.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from . import protocol
from .methods import METHODS, GatewayContext, GatewayError

log = logging.getLogger(__name__)


async def dispatch(req: Any, ctx: GatewayContext) -> Optional[dict[str, Any]]:
    if not isinstance(req, dict):
        return protocol.make_error(None, protocol.ERR_INVALID_REQUEST)

    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    # Notifications (no id): silently accepted.
    if req_id is None and method is not None:
        return None

    if not isinstance(method, str):
        return protocol.make_error(req_id, protocol.ERR_INVALID_REQUEST)

    handler = METHODS.get(method)
    if handler is None:
        return protocol.make_error(req_id, protocol.ERR_METHOD_NOT_FOUND,
                                   message=f"method not found: {method}")

    if not isinstance(params, dict):
        return protocol.make_error(req_id, protocol.ERR_INVALID_PARAMS,
                                   message="params must be an object")

    try:
        result = await handler(params, ctx)
    except GatewayError as e:
        return protocol.make_error(req_id, e.code, e.message)
    except KeyError as e:
        return protocol.make_error(req_id, protocol.ERR_INVALID_PARAMS,
                                   message=f"missing param: {e}")
    except Exception:
        log.exception("gateway dispatch crash method=%s", method)
        return protocol.make_error(req_id, protocol.ERR_INTERNAL_ERROR)
    return protocol.make_response(req_id, result)
