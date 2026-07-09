"""Weixin QR login flow for Admin UI (iLink Bot API)."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from .adapter import ILINK_APP_ID, ILINK_APP_CLIENT_VERSION, ILINK_BASE_URL, WeixinError
from .store import save_account

log = logging.getLogger(__name__)

EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
QR_TIMEOUT_MS = 35_000
QR_SESSION_TTL_SECONDS = 600
MAX_QR_REFRESH = 3


def _api_get_headers() -> dict[str, str]:
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }


async def _api_get(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"

    async def _do() -> dict[str, Any]:
        async with session.get(url, headers=_api_get_headers()) as response:
            raw = await response.text()
            if not response.ok:
                raise WeixinError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
            return __import__("json").loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


def _make_qr_svg(data: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg

        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(data, image_factory=factory, box_size=6, border=2)
        if hasattr(img, "to_string"):
            return img.to_string().decode("utf-8")
        from io import BytesIO
        buf = BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception as exc:
        log.debug("weixin: SVG QR render failed: %s", exc)
        return ""


@dataclass
class QrSession:
    session_id: str
    qrcode_value: str
    qrcode_url: str
    qr_scan_data: str
    current_base_url: str
    status: str = "wait"
    refresh_count: int = 0
    created_at: float = field(default_factory=time.time)
    credentials: dict[str, str] | None = None
    message: str = ""


_sessions: dict[str, QrSession] = {}


def _purge_expired_sessions() -> None:
    cutoff = time.time() - QR_SESSION_TTL_SECONDS
    expired = [sid for sid, s in _sessions.items() if s.created_at < cutoff]
    for sid in expired:
        _sessions.pop(sid, None)


async def _fetch_qr(session: aiohttp.ClientSession, *, bot_type: str = "3") -> dict[str, str]:
    qr_resp = await _api_get(
        session,
        base_url=ILINK_BASE_URL,
        endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
        timeout_ms=QR_TIMEOUT_MS,
    )
    qrcode_value = str(qr_resp.get("qrcode") or "")
    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
    if not qrcode_value:
        raise WeixinError("iLink QR response missing qrcode")
    qr_scan_data = qrcode_url or qrcode_value
    return {
        "qrcode_value": qrcode_value,
        "qrcode_url": qrcode_url,
        "qr_scan_data": qr_scan_data,
    }


async def start_qr_session(*, bot_type: str = "3") -> dict[str, Any]:
    """Create a QR login session and return display payload for the Admin UI."""
    _purge_expired_sessions()
    timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
    async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
        qr = await _fetch_qr(session, bot_type=bot_type)
    session_id = secrets.token_urlsafe(16)
    entry = QrSession(
        session_id=session_id,
        qrcode_value=qr["qrcode_value"],
        qrcode_url=qr["qrcode_url"],
        qr_scan_data=qr["qr_scan_data"],
        current_base_url=ILINK_BASE_URL,
        message="请使用微信扫描二维码",
    )
    _sessions[session_id] = entry
    svg = _make_qr_svg(entry.qr_scan_data)
    return {
        "session_id": session_id,
        "status": entry.status,
        "qrcode_url": entry.qrcode_url,
        "qr_svg": svg,
        "message": entry.message,
        "expires_in": QR_SESSION_TTL_SECONDS,
    }


async def poll_qr_session(session_id: str) -> dict[str, Any]:
    """Poll iLink once for QR login status."""
    _purge_expired_sessions()
    entry = _sessions.get(session_id)
    if entry is None:
        return {"status": "expired", "message": "二维码会话已过期，请重新获取"}

    timeout = aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
    async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
        try:
            status_resp = await _api_get(
                session,
                base_url=entry.current_base_url,
                endpoint=f"{EP_GET_QR_STATUS}?qrcode={entry.qrcode_value}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except asyncio.TimeoutError:
            return {
                "session_id": session_id,
                "status": entry.status,
                "message": entry.message or "等待扫码",
            }
        except Exception as exc:
            log.warning("weixin: QR poll error session=%s: %s", session_id[:8], exc)
            return {"session_id": session_id, "status": "error", "message": str(exc)}

    status = str(status_resp.get("status") or "wait")
    entry.status = status

    if status == "wait":
        entry.message = "等待扫码"
    elif status == "scaned":
        entry.message = "已扫码，请在微信里确认登录"
    elif status == "scaned_but_redirect":
        redirect_host = str(status_resp.get("redirect_host") or "")
        if redirect_host:
            entry.current_base_url = f"https://{redirect_host}"
        entry.message = "已扫码，正在连接..."
    elif status == "expired":
        entry.refresh_count += 1
        if entry.refresh_count > MAX_QR_REFRESH:
            entry.message = "二维码多次过期，请重新获取"
            _sessions.pop(session_id, None)
            return {"session_id": session_id, "status": "expired", "message": entry.message}
        try:
            qr = await _fetch_qr(session)
            entry.qrcode_value = qr["qrcode_value"]
            entry.qrcode_url = qr["qrcode_url"]
            entry.qr_scan_data = qr["qr_scan_data"]
            entry.status = "wait"
            entry.message = "二维码已刷新，请重新扫码"
            svg = _make_qr_svg(entry.qr_scan_data)
            return {
                "session_id": session_id,
                "status": "wait",
                "message": entry.message,
                "qrcode_url": entry.qrcode_url,
                "qr_svg": svg,
            }
        except Exception as exc:
            entry.message = f"刷新二维码失败: {exc}"
            return {"session_id": session_id, "status": "error", "message": entry.message}
    elif status == "confirmed":
        account_id = str(status_resp.get("ilink_bot_id") or "")
        token = str(status_resp.get("bot_token") or "")
        base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
        user_id = str(status_resp.get("ilink_user_id") or "")
        if not account_id or not token:
            entry.message = "登录成功但凭证不完整"
            return {"session_id": session_id, "status": "error", "message": entry.message}
        entry.credentials = {
            "account_id": account_id,
            "token": token,
            "base_url": base_url.rstrip("/"),
            "user_id": user_id,
        }
        entry.message = f"微信连接成功，account_id={account_id}"
        _sessions.pop(session_id, None)
        return {
            "session_id": session_id,
            "status": "confirmed",
            "message": entry.message,
            "account_id": account_id,
            "base_url": entry.credentials["base_url"],
            "user_id": user_id,
            "token": token,
        }

    return {
        "session_id": session_id,
        "status": entry.status,
        "message": entry.message,
    }


def persist_qr_credentials(data_dir: Path, credentials: dict[str, str]) -> None:
    save_account(
        data_dir,
        account_id=credentials["account_id"],
        token=credentials["token"],
        base_url=credentials.get("base_url") or ILINK_BASE_URL,
        user_id=credentials.get("user_id") or "",
    )


def clear_qr_sessions() -> None:
    """For tests."""
    _sessions.clear()
