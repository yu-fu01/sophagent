"""DingTalk session webhook helpers with SSRF protection."""

from __future__ import annotations

import re

import httpx

_WEBHOOK_RE = re.compile(r"^https://(?:api|oapi)\.dingtalk\.com/")


def assert_session_webhook_url(url: str) -> None:
    if not url or not _WEBHOOK_RE.match(url):
        raise ValueError(f"disallowed DingTalk webhook host: {url[:80]}")


async def send_markdown(
    client: httpx.AsyncClient,
    session_webhook: str,
    text: str,
    *,
    title: str = "sophagent",
) -> None:
    assert_session_webhook_url(session_webhook)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text[:20000]},
    }
    resp = await client.post(session_webhook, json=payload, timeout=15.0)
    if resp.status_code >= 300:
        raise RuntimeError(f"DingTalk webhook HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        data = resp.json()
    except Exception:
        return
    errcode = data.get("errcode")
    if errcode not in (0, None):
        raise RuntimeError(
            f"DingTalk webhook errcode={errcode}: {data.get('errmsg', resp.text[:200])}"
        )
