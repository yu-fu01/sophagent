"""JSON-RPC 2.0 over WebSocket — frame construction, error codes, event remap.

Wire format: newline-delimited JSON-RPC 2.0, bidirectional. A *request* carries
an ``id`` and expects a matching response; an *event* carries no ``id``.

This module only builds/parses frames. It knows nothing about transports,
sessions, or the agent loop — that keeps the protocol layer trivially testable.

Event name remap
----------------
sophclaw's :class:`AgentRunner` yields UI events under its historical names
(``text_delta`` / ``tool_call`` / ``done`` …). On the WebSocket wire we expose
the spec's public event names (``message.delta`` / ``tool.call`` /
``message.complete`` …) so the RPC contract isn't coupled to internal names.
The original dict is otherwise preserved verbatim inside ``payload``.
"""

from __future__ import annotations

import json
from typing import Any

JSONRPC_VERSION = "2.0"

# JSON-RPC standard error codes.
ERR_PARSE_ERROR = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INTERNAL_ERROR = -32603
ERR_INVALID_PARAMS = -32602

# Business error codes (4xxx namespace).
ERR_UNAUTHORIZED = 4001
ERR_FORBIDDEN = 4401          # session does not belong to this user
ERR_BUSY = 4009               # guardrail; submit auto-queues so this is rare

_ERROR_MESSAGES = {
    ERR_PARSE_ERROR: "parse error",
    ERR_INVALID_REQUEST: "invalid request",
    ERR_METHOD_NOT_FOUND: "method not found",
    ERR_INVALID_PARAMS: "invalid params",
    ERR_INTERNAL_ERROR: "internal error",
    ERR_UNAUTHORIZED: "unauthorized",
    ERR_FORBIDDEN: "session not owned by caller",
    ERR_BUSY: "session is busy",
}


# Internal sophclaw event type → wire event type.
_EVENT_REMAP: dict[str, str] = {
    "text_delta": "message.delta",
    "reasoning_delta": "reasoning.delta",
    "tool_call": "tool.call",
    "tool_result": "tool.result",
    "done": "message.complete",
    "error": "error",
    "queued_next": "queued_next",
    "turn_usage": "turn.usage",
    "settled": "turn.settled",
    "review": "memory.review",
}


def remap_event(ev: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return ``(wire_type, payload)`` for an internal agent event dict.

    The whole internal event (type included) is carried in ``payload`` so the
    frontend has the original fields; ``type`` is the renamed wire contract.
    """
    internal = ev.get("type", "event")
    wire = _EVENT_REMAP.get(internal, internal)
    return wire, dict(ev)


# ── frame builders ──────────────────────────────────────────────────────────

def make_response(req_id: Any, result: Any = None) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "result": result}


def make_error(req_id: Any, code: int, message: str | None = None,
               data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message or _ERROR_MESSAGES.get(code, "error")}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": req_id, "error": err}


def make_event(wire_type: str, session_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    """A server→client notification (no id)."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "method": "event",
        "params": {"type": wire_type, "session_id": session_id, "payload": payload},
    }


def encode(frame: dict[str, Any]) -> str:
    """Serialize a frame to a newline-terminated-free JSON line.

    Callers append the record separator; this returns the bare JSON string with
    ``ensure_ascii=False`` so CJK text survives.
    """
    return json.dumps(frame, ensure_ascii=False)


def parse_request(raw: str) -> dict[str, Any]:
    """Parse one inbound JSON line. Raises json.JSONDecodeError on bad JSON."""
    return json.loads(raw)
