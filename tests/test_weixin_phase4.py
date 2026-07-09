"""Tests for Weixin Phase 4 hardening."""

import os
from pathlib import Path

import pytest

from sophagent.im.platforms.weixin.cdn import assert_weixin_cdn_url
from sophagent.im.platforms.weixin.health import (
    clear_health,
    mark_lock_blocked,
    mark_session_expired,
    public_health_view,
)
from sophagent.im.platforms.weixin.lock import acquire_lock, release_lock
from sophagent.im.platforms.weixin.paths import resolve_outbound_path


class TestTokenLock:
    def test_acquire_and_release(self, tmp_path: Path):
        ok, _ = acquire_lock(tmp_path, "weixin-bot-token", "tok-1")
        assert ok is True
        release_lock(tmp_path, "weixin-bot-token", "tok-1")
        ok3, _ = acquire_lock(tmp_path, "weixin-bot-token", "tok-1")
        assert ok3 is True
        release_lock(tmp_path, "weixin-bot-token", "tok-1")

    def test_blocks_when_holder_alive(self, tmp_path: Path, monkeypatch):
        from sophagent.im.platforms.weixin.lock import lock_path
        import json
        import time

        holder = 424242
        monkeypatch.setattr(
            "sophagent.im.platforms.weixin.lock._pid_alive",
            lambda pid: pid == holder,
        )
        path = lock_path(tmp_path, "weixin-bot-token", "tok-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": holder,
            "scope": "weixin-bot-token",
            "metadata": {},
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }), encoding="utf-8")
        ok, existing = acquire_lock(tmp_path, "weixin-bot-token", "tok-1")
        assert ok is False
        assert existing is not None


class TestHealth:
    def test_session_expired_marker(self, tmp_path: Path):
        mark_session_expired(tmp_path, "acct1")
        view = public_health_view(tmp_path, "acct1")
        assert view["status"] == "session_expired"
        assert view["needs_relogin"] is True
        clear_health(tmp_path, "acct1")
        assert public_health_view(tmp_path, "acct1")["status"] == "ok"

    def test_lock_blocked_marker(self, tmp_path: Path):
        mark_lock_blocked(tmp_path, "acct1", holder_pid=12345)
        view = public_health_view(tmp_path, "acct1")
        assert view["status"] == "lock_blocked"
        assert view["needs_relogin"] is False


class TestPathSafety:
    def test_resolve_under_workspace(self, tmp_path: Path):
        ws = tmp_path / "workspaces" / "1"
        ws.mkdir(parents=True)
        f = ws / "im" / "weixin" / "a.png"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"png")
        resolved = resolve_outbound_path(str(f), allowed_roots=[tmp_path / "workspaces", tmp_path])
        assert resolved == f.resolve()

    def test_reject_outside_roots(self, tmp_path: Path):
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        assert resolve_outbound_path(str(outside), allowed_roots=[tmp_path / "workspaces"]) is None


class TestCdnAllowlist:
    def test_reject_unknown_host(self):
        with pytest.raises(ValueError):
            assert_weixin_cdn_url("https://evil.example.com/file")

    def test_allow_weixin_cdn(self):
        assert_weixin_cdn_url("https://novac2c.cdn.weixin.qq.com/c2c/download?x=1")
