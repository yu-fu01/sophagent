"""exec 沙箱（Landlock）测试：python_exec/terminal 子进程只能读 workspace，
读不到 workspace 之外的密钥文件（data/providers.yaml、.secret、项目源码）。

Landlock 不可用的内核上整组跳过——沙箱是 best-effort 硬化层，缺它时由
工具输出脱敏兜底（见 test_redact_tool_output.py）。
"""
import pytest

from sophagent.tools import registry
from sophagent.tools.sandbox import landlock_available

requires_landlock = pytest.mark.skipif(
    not landlock_available(), reason="Landlock 在此内核不可用"
)

SECRET = "Sophnet-SUPERSECRET-api-key-value-do-not-leak"


def _plant_secret(tmp_path):
    """在 workspace 之外（兄弟目录 data/）放一个密钥文件，模拟 providers.yaml。"""
    secret = tmp_path / "data" / "providers.yaml"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text(f"api_key: {SECRET}\n", encoding="utf-8")
    return secret


@requires_landlock
async def test_python_exec_cannot_read_outside_workspace(ctx, tmp_path):
    _plant_secret(tmp_path)
    # workspace=tmp_path/ws；../data/providers.yaml 是逃逸路径
    out = await registry.dispatch(
        "python_exec",
        {"code": "print(open('../data/providers.yaml').read())"},
        ctx,
    )
    assert SECRET not in out
    assert "Permission" in out or "denied" in out or "Errno 13" in out


@requires_landlock
async def test_python_exec_cannot_read_absolute_secret_path(ctx, tmp_path):
    secret = _plant_secret(tmp_path)
    out = await registry.dispatch(
        "python_exec",
        {"code": f"print(open({str(secret)!r}).read())"},
        ctx,
    )
    assert SECRET not in out


@requires_landlock
async def test_terminal_cannot_read_outside_workspace(ctx, tmp_path):
    _plant_secret(tmp_path)
    out = await registry.dispatch(
        "terminal", {"command": "cat ../data/providers.yaml"}, ctx
    )
    assert SECRET not in out


@requires_landlock
async def test_python_exec_can_still_read_inside_workspace(ctx):
    (ctx.workspace / "mine.txt").write_text("workspace-data-ok", encoding="utf-8")
    out = await registry.dispatch(
        "python_exec", {"code": "print(open('mine.txt').read())"}, ctx
    )
    assert "workspace-data-ok" in out


@requires_landlock
async def test_python_exec_still_computes(ctx):
    out = await registry.dispatch("python_exec", {"code": "print(6*7)"}, ctx)
    assert "42" in out


@requires_landlock
async def test_terminal_still_runs_normal_command(ctx):
    out = await registry.dispatch("terminal", {"command": "echo hello-world"}, ctx)
    assert "hello-world" in out


# -- 启动状态串：运维可一眼确认走的是沙箱还是降级 --------------------------

def test_sandbox_status_active(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb, "landlock_available", lambda: True)
    msg = sb.sandbox_status()
    assert "active" in msg.lower() and "landlock" in msg.lower()


def test_sandbox_status_degraded(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb, "landlock_available", lambda: False)
    msg = sb.sandbox_status()
    assert "degraded" in msg.lower()


# -- exec_credentials：降权凭据探测 ------------------------------------------

def test_exec_credentials_none_when_not_root(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb.os, "geteuid", lambda: 1000)
    assert sb.exec_credentials() is None


def test_exec_credentials_returns_uid_gid_when_root_and_user_exists(monkeypatch):
    import sophagent.tools.sandbox as sb
    import pwd
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    fake = pwd.struct_passwd(("sandbox", "x", 1001, 1002, "", "/nonexistent", "/usr/sbin/nologin"))
    monkeypatch.setattr(sb.pwd, "getpwnam", lambda n: fake if n == "sandbox" else (_ for _ in ()).throw(KeyError(n)))
    assert sb.exec_credentials() == (1001, 1002)


def test_exec_credentials_none_when_root_but_user_missing(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    def _raise(n): raise KeyError(n)
    monkeypatch.setattr(sb.pwd, "getpwnam", _raise)
    assert sb.exec_credentials() is None


def test_exec_credentials_honors_env_user(monkeypatch):
    import sophagent.tools.sandbox as sb
    import pwd
    monkeypatch.setenv("SOPHAGENT_EXEC_USER", "worker")
    monkeypatch.setattr(sb.os, "geteuid", lambda: 0)
    fake = pwd.struct_passwd(("worker", "x", 2000, 2000, "", "/nonexistent", "/usr/sbin/nologin"))
    monkeypatch.setattr(sb.pwd, "getpwnam", lambda n: fake if n == "worker" else (_ for _ in ()).throw(KeyError(n)))
    assert sb.exec_credentials() == (2000, 2000)


def test_sandbox_status_reports_uid_isolation(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb, "landlock_available", lambda: True)
    monkeypatch.setattr(sb, "exec_credentials", lambda: (1001, 1001))
    msg = sb.sandbox_status()
    assert "uid-isolation=active" in msg and "landlock=active" in msg


def test_sandbox_status_uid_unavailable(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb, "landlock_available", lambda: False)
    monkeypatch.setattr(sb, "exec_credentials", lambda: None)
    msg = sb.sandbox_status()
    assert "uid-isolation=unavailable" in msg and "landlock=degraded" in msg
