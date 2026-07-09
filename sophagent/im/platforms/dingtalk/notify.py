"""DingTalk static robot webhook notifications (cron / alerts)."""

from __future__ import annotations

import logging

import httpx

from .webhook import assert_session_webhook_url

log = logging.getLogger(__name__)


async def send_static_webhook_text(
    webhook_url: str,
    text: str,
    *,
    title: str = "sophagent",
    client: httpx.AsyncClient | None = None,
) -> None:
    """Send a markdown notification via a static robot webhook URL."""
    url = (webhook_url or "").strip()
    if not url or not (text or "").strip():
        return
    assert_session_webhook_url(url)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text[:20000]},
    }
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await http.post(url, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"DingTalk webhook HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"DingTalk API error: {data.get('errmsg', 'unknown')}")
    finally:
        if owns_client:
            await http.aclose()
