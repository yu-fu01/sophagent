"""Weixin runtime health markers (session expiry, lock conflicts)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .store import _atomic_json_write


def health_path(data_dir: Path, account_id: str) -> Path:
    return data_dir / "weixin" / "accounts" / f"{account_id}.health.json"


def load_health(data_dir: Path, account_id: str) -> dict[str, Any]:
    path = health_path(data_dir, account_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_health(data_dir: Path, account_id: str, payload: dict[str, Any]) -> None:
    if not account_id:
        return
    _atomic_json_write(health_path(data_dir, account_id), payload)


def clear_health(data_dir: Path, account_id: str) -> None:
    path = health_path(data_dir, account_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def mark_session_expired(data_dir: Path, account_id: str, *, detail: str = "") -> None:
    save_health(data_dir, account_id, {
        "status": "session_expired",
        "needs_relogin": True,
        "message": detail or "微信 iLink 会话已过期，请在 Admin IM 页面重新扫码登录。",
        "marked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def mark_lock_blocked(data_dir: Path, account_id: str, *, holder_pid: int | None = None) -> None:
    save_health(data_dir, account_id, {
        "status": "lock_blocked",
        "needs_relogin": False,
        "message": "同一微信 token 已被其他 sophagent 实例占用，本实例未启动轮询。",
        "holder_pid": holder_pid,
        "marked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def public_health_view(data_dir: Path, account_id: str) -> dict[str, Any]:
    data = load_health(data_dir, account_id)
    if not data:
        return {
            "status": "ok",
            "needs_relogin": False,
            "message": "",
        }
    return {
        "status": str(data.get("status") or "unknown"),
        "needs_relogin": bool(data.get("needs_relogin")),
        "message": str(data.get("message") or ""),
        "marked_at": data.get("marked_at"),
    }
