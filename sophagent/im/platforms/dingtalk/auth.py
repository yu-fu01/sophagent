"""DingTalk device-flow QR authorization for Admin UI."""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

REGISTRATION_BASE_URL = os.environ.get(
    "SOPHAGENT_DINGTALK_REGISTRATION_BASE_URL",
    os.environ.get("DINGTALK_REGISTRATION_BASE_URL", "https://oapi.dingtalk.com"),
).rstrip("/")
REGISTRATION_SOURCE = os.environ.get(
    "SOPHAGENT_DINGTALK_REGISTRATION_SOURCE",
    os.environ.get("DINGTALK_REGISTRATION_SOURCE", "openClaw"),
)
QR_SESSION_TTL_SECONDS = 7200


class RegistrationError(Exception):
    pass


@dataclass
class QrSession:
    session_id: str
    device_code: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    status: str = "waiting"
    client_id: str = ""
    client_secret: str = ""
    message: str = ""
    created_at: float = field(default_factory=time.time)


_sessions: dict[str, QrSession] = {}


def _purge_expired_sessions() -> None:
    cutoff = time.time() - QR_SESSION_TTL_SECONDS
    expired = [sid for sid, s in _sessions.items() if s.created_at < cutoff]
    for sid in expired:
        _sessions.pop(sid, None)


async def _api_post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{REGISTRATION_BASE_URL}{path}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    errcode = data.get("errcode", -1)
    if errcode != 0:
        raise RegistrationError(
            f"{data.get('errmsg', 'unknown error')} (errcode={errcode})"
        )
    return data


def _make_qr_svg(url: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg

        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(url, image_factory=factory, box_size=6, border=2)
        if hasattr(img, "to_string"):
            return img.to_string().decode("utf-8")
        from io import BytesIO
        buf = BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception as exc:
        log.debug("dingtalk: SVG QR render failed: %s", exc)
        return ""


async def begin_registration() -> dict[str, Any]:
    init_data = await _api_post("/app/registration/init", {"source": REGISTRATION_SOURCE})
    nonce = str(init_data.get("nonce") or "").strip()
    if not nonce:
        raise RegistrationError("init response missing nonce")
    begin_data = await _api_post("/app/registration/begin", {"nonce": nonce})
    device_code = str(begin_data.get("device_code") or "").strip()
    verification_uri_complete = str(begin_data.get("verification_uri_complete") or "").strip()
    if not device_code or not verification_uri_complete:
        raise RegistrationError("begin response missing device_code or verification_uri_complete")
    return {
        "device_code": device_code,
        "verification_uri_complete": verification_uri_complete,
        "expires_in": int(begin_data.get("expires_in", 7200)),
        "interval": max(int(begin_data.get("interval", 3)), 2),
    }


async def poll_registration(device_code: str) -> dict[str, Any]:
    data = await _api_post("/app/registration/poll", {"device_code": device_code})
    status_raw = str(data.get("status") or "").strip().upper()
    if status_raw not in {"WAITING", "SUCCESS", "FAIL", "EXPIRED"}:
        status_raw = "UNKNOWN"
    return {
        "status": status_raw,
        "client_id": str(data.get("client_id") or "").strip() or None,
        "client_secret": str(data.get("client_secret") or "").strip() or None,
        "fail_reason": str(data.get("fail_reason") or "").strip() or None,
    }


async def start_qr_session() -> dict[str, Any]:
    _purge_expired_sessions()
    reg = await begin_registration()
    session_id = secrets.token_urlsafe(16)
    qr_svg = _make_qr_svg(reg["verification_uri_complete"])
    _sessions[session_id] = QrSession(
        session_id=session_id,
        device_code=reg["device_code"],
        verification_uri_complete=reg["verification_uri_complete"],
        expires_in=reg["expires_in"],
        interval=reg["interval"],
        message="请使用钉钉扫描二维码完成授权（页面可能显示 OpenClaw 品牌）",
    )
    return {
        "session_id": session_id,
        "status": "waiting",
        "qr_svg": qr_svg,
        "verification_uri": reg["verification_uri_complete"],
        "message": _sessions[session_id].message,
        "expires_in": reg["expires_in"],
    }


async def poll_qr_session(session_id: str) -> dict[str, Any]:
    _purge_expired_sessions()
    session = _sessions.get(session_id)
    if session is None:
        return {"status": "expired", "message": "扫码会话已过期，请重新获取二维码"}
    try:
        result = await poll_registration(session.device_code)
    except RegistrationError as exc:
        return {"status": "error", "message": str(exc)}

    status = result["status"]
    if status == "WAITING":
        return {
            "status": "waiting",
            "message": session.message or "等待扫码授权...",
        }
    if status == "SUCCESS":
        client_id = result.get("client_id") or ""
        client_secret = result.get("client_secret") or ""
        if not client_id or not client_secret:
            return {"status": "error", "message": "授权成功但凭证缺失"}
        session.status = "confirmed"
        session.client_id = client_id
        session.client_secret = client_secret
        return {
            "status": "confirmed",
            "message": "钉钉授权成功",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    reason = result.get("fail_reason") or status
    session.status = "failed"
    return {"status": "failed", "message": f"授权失败：{reason}"}
