"""File-based scoped locks for Weixin bot token mutual exclusion."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


def _hash_identity(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def lock_path(data_dir: Path, scope: str, identity: str) -> Path:
    return data_dir / "weixin" / "locks" / f"{scope}-{_hash_identity(identity)}.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def acquire_lock(data_dir: Path, scope: str, identity: str, *, metadata: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any] | None]:
    """Return (acquired, existing_record). Only one live process per scope+identity."""
    if not identity:
        return True, None
    path = lock_path(data_dir, scope, identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "pid": os.getpid(),
        "scope": scope,
        "metadata": metadata or {},
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    existing = _read_lock(path)
    if existing:
        existing_pid = existing.get("pid")
        try:
            existing_pid = int(existing_pid)
        except (TypeError, ValueError):
            existing_pid = None
        if existing_pid == os.getpid():
            path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            return True, existing
        if existing_pid is not None and _pid_alive(existing_pid):
            return False, existing
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False, _read_lock(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True, None


def release_lock(data_dir: Path, scope: str, identity: str) -> None:
    if not identity:
        return
    path = lock_path(data_dir, scope, identity)
    existing = _read_lock(path)
    if not existing:
        return
    if existing.get("pid") != os.getpid():
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
