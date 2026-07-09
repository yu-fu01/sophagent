"""Feishu webhook HTTP entrypoint."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request

from ..im.platforms.feishu import handle_feishu_event_dict

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhook")
async def feishu_webhook(request: Request):
    body = await request.body()
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {"code": 400, "msg": "invalid json"}

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    await handle_feishu_event_dict(payload)
    return {"code": 0, "msg": "ok"}
