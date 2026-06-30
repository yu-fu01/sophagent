"""Shell and Python execution tools.

Security model: the container is the hard boundary. Within it, commands run
as subprocesses with cwd pinned to the user's workspace, a whitelisted
environment (no host secrets), a timeout and output caps. See README for the
multi-user soft-isolation caveat; disable these tools on sensitive agents.
"""

from __future__ import annotations

import asyncio
import os
import uuid

from ..config import get_config
from .registry import ToolContext, tool, truncate
from .sandbox import exec_argv, exec_credentials

ENV_WHITELIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")
MAX_TIMEOUT = 300.0


def _safe_env() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_WHITELIST if k in os.environ}


async def _run_subprocess(cmd: str, cwd: str, timeout: float) -> str:
    creds = exec_credentials()
    extra: dict = {}
    if creds is not None:
        extra["user"], extra["group"] = creds
    proc = await asyncio.create_subprocess_exec(
        *exec_argv(cmd, cwd),
        cwd=cwd,
        env=_safe_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,  # own process group so we can kill children
        **extra,
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
    try:
        return await _run_subprocess(f"python3 {script.name}", str(ctx.workspace), timeout)
    finally:
        script.unlink(missing_ok=True)
