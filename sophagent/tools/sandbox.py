"""Landlock-based filesystem sandbox for the exec tools.

The container is the hard boundary; within it, ``terminal`` / ``python_exec``
historically ran as the same OS user with full filesystem read access — so a
subprocess could read provider keys out of the data dir (``providers.yaml``,
``.secret``, the sqlite DB) or the project source. This module confines the
exec subprocess with Linux Landlock so it can only *read* the workspace plus
the language runtime; everything else (the data dir, project source, $HOME) is
denied at the kernel level, unprivileged and per-exec.

Graceful by design: on kernels without Landlock the helpers are no-ops and the
tool-output redaction layer remains the backstop.
"""
from __future__ import annotations

import ctypes
import os
import pwd
import sys
from pathlib import Path

# syscall numbers (x86_64/arm64 share these)
_NR_create_ruleset = 444

_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

_LAUNCHER = str(Path(__file__).with_name("_sandbox_exec.py"))


def _libc() -> ctypes.CDLL | None:
    try:
        return ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return None


def landlock_available() -> bool:
    """True iff this kernel exposes a usable Landlock ABI (>= 1)."""
    libc = _libc()
    if libc is None:
        return False
    try:
        abi = libc.syscall(
            _NR_create_ruleset, None, ctypes.c_size_t(0),
            ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except OSError:
        return False
    return abi >= 1


def exec_credentials() -> "tuple[int, int] | None":
    """(uid, gid) to drop the exec subprocess to, or None if uid isolation
    isn't available here.

    Available only when we are root (can setuid) AND the dedicated low-priv
    user exists (default ``sandbox``, override via ``SOPHAGENT_EXEC_USER``).
    Non-root dev runs and misconfigured images return None → caller skips
    ``user=`` and falls back to the Landlock / redaction layers."""
    if os.geteuid() != 0:
        return None
    name = os.environ.get("SOPHAGENT_EXEC_USER", "sandbox")
    try:
        ent = pwd.getpwnam(name)
    except KeyError:
        return None
    return (ent.pw_uid, ent.pw_gid)


def exec_argv(command: str, workspace: str) -> list[str]:
    """argv that runs ``command`` (shell-interpreted) under the active sandbox.

    When uid isolation and/or Landlock applies, route through the launcher
    ``_sandbox_exec.py <uid|-> <gid|-> <landlock> <ws> -- /bin/sh -c <cmd>`` which
    drops privileges and/or confines reads *inside the launched process*. This is
    deliberately NOT done via ``create_subprocess_exec(user=,group=)``: uvloop
    (uvicorn's production loop) rejects those kwargs. When neither layer applies
    (local non-root dev on an old kernel), return a plain ``/bin/sh -c``."""
    base = ["/bin/sh", "-c", command]
    creds = exec_credentials()
    landlock = landlock_available()
    if creds is None and not landlock:
        return base
    uid = str(creds[0]) if creds else "-"
    gid = str(creds[1]) if creds else "-"
    ll = "1" if landlock else "0"
    return [sys.executable, _LAUNCHER, uid, gid, ll, str(workspace), "--", *base]


def sandbox_status() -> str:
    """One-line, log-friendly summary of the exec sandbox's effective state.

    Logged once at startup so operators can confirm which protection layers are
    actually in force: uid isolation (OS-user, any kernel) and Landlock
    (kernel/seccomp dependent). Both degrade silently otherwise."""
    uid = "active" if exec_credentials() is not None else "unavailable"
    landlock = "active" if landlock_available() else "degraded"
    return f"exec sandbox: uid-isolation={uid}, landlock={landlock}"
