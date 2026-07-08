"""Tests for Weixin QR auth helpers."""

import pytest

from sophagent.im.platforms.weixin.auth import clear_qr_sessions, poll_qr_session, start_qr_session


@pytest.mark.asyncio
async def test_start_and_poll_qr_session_mocked(monkeypatch):
    clear_qr_sessions()

    async def fake_fetch(session, *, bot_type="3"):
        return {
            "qrcode_value": "qr-val",
            "qrcode_url": "https://example.com/qr",
            "qr_scan_data": "https://example.com/qr",
        }

    poll_count = {"n": 0}

    async def fake_api_get(session, *, base_url, endpoint, timeout_ms):
        poll_count["n"] += 1
        if "get_qrcode_status" in endpoint:
            return {
                "status": "confirmed",
                "ilink_bot_id": "acct-x",
                "bot_token": "tok-x",
                "baseurl": "https://ilinkai.weixin.qq.com",
                "ilink_user_id": "user-x",
            }
        raise AssertionError(endpoint)

    monkeypatch.setattr("sophagent.im.platforms.weixin.auth._fetch_qr", fake_fetch)
    monkeypatch.setattr("sophagent.im.platforms.weixin.auth._api_get", fake_api_get)
    monkeypatch.setattr("sophagent.im.platforms.weixin.auth._make_qr_svg", lambda data: "<svg/>")

    started = await start_qr_session()
    assert started["session_id"]
    assert started["status"] == "wait"

    result = await poll_qr_session(started["session_id"])
    assert result["status"] == "confirmed"
    assert result["account_id"] == "acct-x"
    assert result["token"] == "tok-x"
    assert poll_count["n"] == 1
