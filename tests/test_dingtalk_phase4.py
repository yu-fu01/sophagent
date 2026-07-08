"""Tests for DingTalk Phase 4 hardening."""

import json
import time
from pathlib import Path

import pytest

from sophagent.im.platforms.dingtalk.health import (
    clear_health,
    mark_credentials_invalid,
    mark_lock_blocked,
    public_health_view,
)
from sophagent.im.platforms.dingtalk.lock import acquire_lock, lock_path, release_lock
from sophagent.im.platforms.dingtalk.media import assert_download_url
from sophagent.im.platforms.dingtalk.paths import resolve_outbound_path
from sophagent.im.platforms.dingtalk.webhook import assert_session_webhook_url


class TestTokenLock:
    def test_acquire_and_release(self, tmp_path: Path):
        ok, _ = acquire_lock(tmp_path, "dingtalk-stream", "app-key-1")
        assert ok is True
        release_lock(tmp_path, "dingtalk-stream", "app-key-1")
        ok3, _ = acquire_lock(tmp_path, "dingtalk-stream", "app-key-1")
        assert ok3 is True
        release_lock(tmp_path, "dingtalk-stream", "app-key-1")

    def test_blocks_when_holder_alive(self, tmp_path: Path, monkeypatch):
        holder = 424242
        monkeypatch.setattr(
            "sophagent.im.platforms.dingtalk.lock._pid_alive",
            lambda pid: pid == holder,
        )
        path = lock_path(tmp_path, "dingtalk-stream", "app-key-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": holder,
            "scope": "dingtalk-stream",
            "metadata": {},
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }), encoding="utf-8")
        ok, existing = acquire_lock(tmp_path, "dingtalk-stream", "app-key-1")
        assert ok is False
        assert existing is not None


class TestHealth:
    def test_credentials_invalid_marker(self, tmp_path: Path):
        mark_credentials_invalid(tmp_path, "app-key-1")
        view = public_health_view(tmp_path, "app-key-1")
        assert view["status"] == "credentials_invalid"
        assert view["needs_relogin"] is True
        clear_health(tmp_path, "app-key-1")
        assert public_health_view(tmp_path, "app-key-1")["status"] == "ok"

    def test_lock_blocked_marker(self, tmp_path: Path):
        mark_lock_blocked(tmp_path, "app-key-1", holder_pid=12345)
        view = public_health_view(tmp_path, "app-key-1")
        assert view["status"] == "lock_blocked"
        assert view["needs_relogin"] is False


class TestPathSafety:
    def test_resolve_under_workspace(self, tmp_path: Path):
        ws = tmp_path / "workspaces" / "1"
        ws.mkdir(parents=True)
        f = ws / "im" / "dingtalk" / "a.png"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"png")
        resolved = resolve_outbound_path(str(f), allowed_roots=[tmp_path / "workspaces", tmp_path])
        assert resolved == f.resolve()

    def test_reject_outside_roots(self, tmp_path: Path):
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        assert resolve_outbound_path(str(outside), allowed_roots=[tmp_path / "workspaces"]) is None


class TestUrlAllowlist:
    def test_session_webhook_allow(self):
        assert_session_webhook_url("https://oapi.dingtalk.com/robot/send?access_token=x")

    def test_session_webhook_reject(self):
        with pytest.raises(ValueError):
            assert_session_webhook_url("https://evil.example.com/hook")

    def test_download_url_allow(self):
        assert_download_url("https://down.dingtalk.com/c2c/abc")

    def test_download_url_reject(self):
        with pytest.raises(ValueError):
            assert_download_url("https://evil.example.com/x")
