"""Shell and Python execution tools.

Security model: the container is the hard boundary. Within it, commands run
as subprocesses with cwd pinned to the user's workspace, a whitelisted
environment (no host secrets), a timeout and output caps. See README for the
multi-user soft-isolation caveat; disable these tools on sensitive agents.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid

from ..config import get_config
from .registry import ToolContext, tool, truncate
from .sandbox import exec_argv

ENV_WHITELIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")
MAX_TIMEOUT = 300.0


def _safe_env() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_WHITELIST if k in os.environ}


async def _run_subprocess(cmd: str, cwd: str, timeout: float) -> str:
    # Privilege drop + Landlock are applied inside exec_argv's launcher process,
    # NOT via user=/group= kwargs — uvloop (uvicorn's loop) rejects those.
    proc = await asyncio.create_subprocess_exec(
        *exec_argv(cmd, cwd),
        cwd=cwd,
        env=_safe_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # own process group so we can kill children
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.wait()
        return f"Error: command timed out after {timeout:.0f}s"
    text = out.decode("utf-8", errors="replace")
    result = truncate(text) if text.strip() else "(no output)"
    if proc.returncode != 0:
        result += f"\n[exit code: {proc.returncode}]"
    return result


@tool(
    "terminal",
    "Run a shell command in your workspace directory. Output (stdout+stderr) is returned, truncated if long.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number", "description": "Seconds, default 60, max 300"},
        },
        "required": ["command"],
    },
)
async def terminal(ctx: ToolContext, command: str, timeout: float = 0) -> str:
    timeout = min(timeout or get_config().tool_timeout, MAX_TIMEOUT)
    return await _run_subprocess(command, str(ctx.workspace), timeout)


@tool(
    "python_exec",
    "Execute a Python script (python3) in your workspace and return its output.",
    {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Python source to run"},
            "timeout": {"type": "number", "description": "Seconds, default 60, max 300"},
        },
        "required": ["code"],
    },
)
async def python_exec(ctx: ToolContext, code: str, timeout: float = 0) -> str:
    timeout = min(timeout or get_config().tool_timeout, MAX_TIMEOUT)
    script = ctx.workspace / f".pyexec_{uuid.uuid4().hex[:8]}.py"
    script.write_text(code, encoding="utf-8")
    script.chmod(0o644)  # 降权后的 sandbox 子进程需可读以执行(不依赖默认 umask)
    try:
        return await _run_subprocess(f"python3 {script.name}", str(ctx.workspace), timeout)
    finally:
        script.unlink(missing_ok=True)


def _selftest_verdict(*, exec_ok: bool, isolation_expected: bool, leaked: bool) -> tuple[bool, str]:
    """Pure verdict for the startup self-test (unit-tested in isolation).

    - exec broken → fail (catches the uvloop-style "claims active but every
      exec crashes" regression).
    - isolation expected (root+sandbox user or Landlock) yet a known
      out-of-sandbox file was readable → fail (catches silent isolation
      degradation where sandbox_status still says active).
    - no isolation layer present (local dev) → a readable out-of-sandbox file
      is expected, not a failure."""
    if not exec_ok:
        return False, "exec 自检失败：工具无法执行命令(沙箱声称生效但 exec 全崩，疑似事件循环/启动器问题)"
    if isolation_expected and leaked:
        return False, "exec 自检失败：隔离声称生效却未能阻止越界读取(isolation 静默失效)"
    detail = "越界读取已被阻止" if isolation_expected else "无隔离层(本地 dev)"
    return True, f"exec 自检通过(exec 正常；{detail})"


async def selftest_exec(cfg) -> tuple[bool, str]:
    """Functionally probe the exec sandbox at startup, through the real
    ``_run_subprocess`` path (so it runs under the production event loop, e.g.
    uvloop). Verifies (a) a command actually executes and (b) a file just
    outside the workspace cannot be read when isolation is expected.

    Returns ``(ok, message)``. Best-effort: any internal error is reported as a
    failed exec verdict rather than raising here (the caller decides whether to
    abort startup)."""
    from .sandbox import exec_credentials, landlock_available

    isolation_expected = exec_credentials() is not None or landlock_available()
    probe_ws = cfg.workspaces_dir / ".sandbox_selftest"
    canary = cfg.data_dir / ".sandbox_selftest_canary"  # sits one level above the workspace
    token = "EXECOK-" + uuid.uuid4().hex[:8]
    canary_token = "CANARYLEAK-" + uuid.uuid4().hex[:12]
    try:
        probe_ws.mkdir(parents=True, exist_ok=True)
        creds = exec_credentials()
        if creds is not None:
            try:
                os.chown(probe_ws, creds[0], creds[1])  # so the dropped user can use it
            except OSError:
                pass
        canary.write_text(canary_token, encoding="utf-8")
        try:
            os.chmod(canary, 0o600)  # root-only; the dropped user must not read it
        except OSError:
            pass
        out_exec = await _run_subprocess(f"echo {token}", str(probe_ws), 10)
        exec_ok = token in out_exec
        out_read = await _run_subprocess(f"cat ../../{canary.name}", str(probe_ws), 10)
        leaked = canary_token in out_read
    except Exception as e:  # noqa: BLE001 — a crashing probe IS an exec failure
        return False, f"exec 自检失败：自检本身抛异常 {type(e).__name__}: {e}"
    finally:
        canary.unlink(missing_ok=True)
        shutil.rmtree(probe_ws, ignore_errors=True)
    return _selftest_verdict(exec_ok=exec_ok, isolation_expected=isolation_expected, leaked=leaked)
