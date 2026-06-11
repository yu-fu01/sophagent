import pytest

import sophclaw.tools  # noqa: F401  (registers file/terminal/web tools)
from sophclaw.tools import registry
from sophclaw.tools.files import safe_path


async def test_dispatch_unknown_tool(ctx):
    out = await registry.dispatch("nope", {}, ctx)
    assert "unknown tool" in out


async def test_dispatch_not_enabled(ctx):
    ctx.agent.tools = ["read_file"]
    out = await registry.dispatch("terminal", {"command": "echo hi"}, ctx)
    assert "not enabled" in out


async def test_dispatch_drops_hallucinated_kwargs(ctx):
    out = await registry.dispatch("list_dir", {"path": ".", "bogus": 1}, ctx)
    assert "Error" not in out


def test_safe_path_rejects_escape(ctx):
    with pytest.raises(ValueError):
        safe_path(ctx.workspace, "../outside.txt")
    with pytest.raises(ValueError):
        safe_path(ctx.workspace, "/etc/passwd")
    assert safe_path(ctx.workspace, "sub/file.txt").is_relative_to(ctx.workspace)


async def test_file_roundtrip(ctx):
    await registry.dispatch("write_file", {"path": "a/b.txt", "content": "hello"}, ctx)
    out = await registry.dispatch("read_file", {"path": "a/b.txt"}, ctx)
    assert out == "hello"
    out = await registry.dispatch(
        "edit_file", {"path": "a/b.txt", "old_string": "hello", "new_string": "world"}, ctx
    )
    assert "Replaced" in out
    assert (ctx.workspace / "a/b.txt").read_text() == "world"


async def test_edit_requires_unique_match(ctx):
    (ctx.workspace / "x.txt").write_text("aa aa")
    out = await registry.dispatch(
        "edit_file", {"path": "x.txt", "old_string": "aa", "new_string": "b"}, ctx
    )
    assert "occurs 2 times" in out


async def test_terminal_runs_in_workspace(ctx):
    out = await registry.dispatch("terminal", {"command": "pwd"}, ctx)
    assert str(ctx.workspace) in out


async def test_terminal_timeout(ctx):
    out = await registry.dispatch("terminal", {"command": "sleep 5", "timeout": 1}, ctx)
    assert "timed out" in out


async def test_terminal_env_whitelist(ctx, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET", "x")
    out = await registry.dispatch("terminal", {"command": "env"}, ctx)
    assert "SUPER_SECRET" not in out


async def test_python_exec(ctx):
    out = await registry.dispatch("python_exec", {"code": "print(6*7)"}, ctx)
    assert "42" in out


async def test_python_exec_error_returned(ctx):
    out = await registry.dispatch("python_exec", {"code": "raise ValueError('boom')"}, ctx)
    assert "boom" in out and "exit code" in out


def test_truncate(ctx):
    long = "x" * 50_000
    out = registry.truncate(long)
    assert len(out) < 35_000 and "truncated" in out


async def test_web_fetch_ssrf_blocked(ctx):
    out = await registry.dispatch("web_fetch", {"url": "http://127.0.0.1:8000/"}, ctx)
    assert "non-public" in out
    out = await registry.dispatch("web_fetch", {"url": "file:///etc/passwd"}, ctx)
    assert "Error" in out


def test_extract_text():
    from sophclaw.tools.web import extract_text

    html = "<html><head><script>bad()</script></head><body><h1>Title</h1><p>Body text</p></body></html>"
    text = extract_text(html)
    assert "Title" in text and "Body text" in text and "bad()" not in text
