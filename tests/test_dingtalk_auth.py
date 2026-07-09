"""DingTalk QR auth tests."""

import pytest

from sophagent.im.platforms.dingtalk import auth as dingtalk_auth


@pytest.mark.asyncio
async def test_poll_qr_session_waiting(monkeypatch):
    session_id = "sess-1"
    dingtalk_auth._sessions[session_id] = dingtalk_auth.QrSession(
        session_id=session_id,
        device_code="dev-code",
        verification_uri_complete="https://example.com/qr",
        expires_in=7200,
        interval=3,
    )

    async def fake_poll(device_code):
        assert device_code == "dev-code"
        return {"status": "WAITING"}

    monkeypatch.setattr(dingtalk_auth, "poll_registration", fake_poll)
    result = await dingtalk_auth.poll_qr_session(session_id)
    assert result["status"] == "waiting"
    dingtalk_auth._sessions.pop(session_id, None)


@pytest.mark.asyncio
async def test_poll_qr_session_confirmed(monkeypatch):
    session_id = "sess-2"
    dingtalk_auth._sessions[session_id] = dingtalk_auth.QrSession(
        session_id=session_id,
        device_code="dev-code",
        verification_uri_complete="https://example.com/qr",
        expires_in=7200,
        interval=3,
    )

    async def fake_poll(device_code):
        return {
            "status": "SUCCESS",
            "client_id": "APPKEY",
            "client_secret": "APPSECRET",
        }

    monkeypatch.setattr(dingtalk_auth, "poll_registration", fake_poll)
    result = await dingtalk_auth.poll_qr_session(session_id)
    assert result["status"] == "confirmed"
    assert result["client_id"] == "APPKEY"
    assert result["client_secret"] == "APPSECRET"
    dingtalk_auth._sessions.pop(session_id, None)
