"""DingTalk runtime health markers (credential errors, lock conflicts)."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .store import atomic_json_write


def _hash_client_id(client_id: str) -> str:
    return hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]


def health_path(data_dir: Path, client_id: str) -> Path:
    return data_dir / "dingtalk" / "health" / f"{_hash_client_id(client_id)}.json"


def load_health(data_dir: Path, client_id: str) -> dict[str, Any]:
    path = health_path(data_dir, client_id)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_health(data_dir: Path, client_id: str, payload: dict[str, Any]) -> None:
    if not client_id:
        return
    atomic_json_write(health_path(data_dir, client_id), payload)


def clear_health(data_dir: Path, client_id: str) -> None:
    path = health_path(data_dir, client_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def mark_credentials_invalid(data_dir: Path, client_id: str, *, detail: str = "") -> None:
    save_health(data_dir, client_id, {
        "status": "credentials_invalid",
        "needs_relogin": True,
        "message": detail or "钉钉 AppKey/AppSecret 无效或已失效，请在 Admin IM 页面重新扫码或更新凭证。",
        "marked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def mark_lock_blocked(data_dir: Path, client_id: str, *, holder_pid: int | None = None) -> None:
    save_health(data_dir, client_id, {
        "status": "lock_blocked",
        "needs_relogin": False,
        "message": "同一钉钉 AppKey 已被其他 sophagent 实例占用，本实例未启动 Stream 连接。",
        "holder_pid": holder_pid,
        "marked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })


def public_health_view(data_dir: Path, client_id: str) -> dict[str, Any]:
    data = load_health(data_dir, client_id)
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
