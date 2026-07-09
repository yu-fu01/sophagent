"""Weixin credential and state persistence under SOPHAGENT_DATA_DIR."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def account_dir(data_dir: Path) -> Path:
    path = data_dir / "weixin" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def account_file(data_dir: Path, account_id: str) -> Path:
    return account_dir(data_dir) / f"{account_id}.json"


def save_account(
    data_dir: Path,
    *,
    account_id: str,
    token: str,
    base_url: str,
    user_id: str = "",
) -> None:
    payload = {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = account_file(data_dir, account_id)
    _atomic_json_write(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_account(data_dir: Path, account_id: str) -> dict[str, Any] | None:
    path = account_file(data_dir, account_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def sync_buf_path(data_dir: Path, account_id: str) -> Path:
    return account_dir(data_dir) / f"{account_id}.sync.json"


def load_sync_buf(data_dir: Path, account_id: str) -> str:
    path = sync_buf_path(data_dir, account_id)
    if not path.is_file():
        return ""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", "")
    except Exception:
        return ""


def save_sync_buf(data_dir: Path, account_id: str, sync_buf: str) -> None:
    _atomic_json_write(sync_buf_path(data_dir, account_id), {"get_updates_buf": sync_buf})


class ContextTokenStore:
    """Disk-backed context_token cache keyed by account + peer."""

    def __init__(self, data_dir: Path) -> None:
        self._root = account_dir(data_dir)
        self._cache: dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self._root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        path = self._path(account_id)
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("weixin: failed to restore context tokens for %s: %s", account_id[:8], exc)
            return
        restored = 0
        for user_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, user_id)] = token
                restored += 1
        if restored:
            log.info("weixin: restored %d context token(s) for %s", restored, account_id[:8])

    def get(self, account_id: str, user_id: str) -> str | None:
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        self._cache[self._key(account_id, user_id)] = token
        self._persist(account_id)

    def clear(self, account_id: str, user_id: str) -> None:
        self._cache.pop(self._key(account_id, user_id), None)
        self._persist(account_id)

    def _persist(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        payload = {
            key[len(prefix):]: value
            for key, value in self._cache.items()
            if key.startswith(prefix)
        }
        try:
            _atomic_json_write(self._path(account_id), payload)
        except Exception as exc:
            log.warning("weixin: failed to persist context tokens for %s: %s", account_id[:8], exc)
