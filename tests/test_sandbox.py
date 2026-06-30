"""exec 沙箱（Landlock）测试：python_exec/terminal 子进程只能读 workspace，
读不到 workspace 之外的密钥文件（data/providers.yaml、.secret、项目源码）。

Landlock 不可用的内核上整组跳过——沙箱是 best-effort 硬化层，缺它时由
工具输出脱敏兜底（见 test_redact_tool_output.py）。
"""
import pytest

from sophagent.tools import registry
from sophagent.tools.sandbox import landlock_available

try:
    import uvloop  # noqa: F401
    _HAS_UVLOOP = True
except ImportError:
    _HAS_UVLOOP = False

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


async def test_run_subprocess_never_passes_loop_incompatible_kwargs(ctx, monkeypatch):
    """即使有降权凭据，_run_subprocess 也绝不给 create_subprocess_exec 传
    user/group/extra_groups —— 这些会被 uvloop(uvicorn 生产事件循环)拒绝。
    降权改由 exec_argv 的启动器进程内部完成；uid 通过位置参数(argv)编码。"""
    import sophagent.tools.terminal as term
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb, "exec_credentials", lambda: (4321, 8765))  # 假装 root 有凭据
    monkeypatch.setattr(sb, "landlock_available", lambda: False)
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["kwargs"] = kwargs
        captured["argv"] = args
        raise RuntimeError("stop")

    monkeypatch.setattr(term.asyncio, "create_subprocess_exec", fake_exec)
    try:
        await term._run_subprocess("echo hi", str(ctx.workspace), 5)
    except RuntimeError:
        pass
    k = captured["kwargs"]
    assert "user" not in k and "group" not in k and "extra_groups" not in k
    # uid 改由启动器 argv 编码(4321/8765 出现在位置参数里)
    assert any("4321" in str(a) for a in captured["argv"])


def test_exec_argv_plain_when_no_isolation(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb, "exec_credentials", lambda: None)
    monkeypatch.setattr(sb, "landlock_available", lambda: False)
    assert sb.exec_argv("echo hi", "/ws") == ["/bin/sh", "-c", "echo hi"]


def test_exec_argv_landlock_only_no_drop(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb, "exec_credentials", lambda: None)
    monkeypatch.setattr(sb, "landlock_available", lambda: True)
    a = sb.exec_argv("echo hi", "/ws")
    assert a[0] == sb.sys.executable and a[1] == sb._LAUNCHER
    assert a[2:7] == ["-", "-", "1", "/ws", "--"]  # uid=- gid=- landlock=1
    assert a[-3:] == ["/bin/sh", "-c", "echo hi"]


def test_exec_argv_uid_drop_no_landlock(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb, "exec_credentials", lambda: (1001, 1002))
    monkeypatch.setattr(sb, "landlock_available", lambda: False)
    a = sb.exec_argv("echo hi", "/ws")
    assert a[2:7] == ["1001", "1002", "0", "/ws", "--"]  # uid/gid set, landlock off


def test_exec_argv_uid_drop_and_landlock(monkeypatch):
    import sophagent.tools.sandbox as sb
    monkeypatch.setattr(sb, "exec_credentials", lambda: (1001, 1002))
    monkeypatch.setattr(sb, "landlock_available", lambda: True)
    a = sb.exec_argv("echo hi", "/ws")
    assert a[2:7] == ["1001", "1002", "1", "/ws", "--"]  # both layers


@pytest.mark.skipif(
    _HAS_UVLOOP is False,
    reason="uvloop 未安装(生产用,本地可缺)",
)
def test_run_subprocess_works_under_uvloop_loop(ctx):
    """回归守卫:在 uvloop 事件循环下 _run_subprocess 不得因 create_subprocess_exec
    的 kwargs 报错(旧实现传 user/group/extra_groups 会被 uvloop 拒绝)。
    同步测试 + 独立 uvloop 循环,避免与 pytest-asyncio 的循环嵌套。"""
    import uvloop
    import sophagent.tools.terminal as term

    async def go():
        return await term._run_subprocess("echo uvloop-ok", str(ctx.workspace), 10)

    loop = uvloop.new_event_loop()
    try:
        out = loop.run_until_complete(go())
    finally:
        loop.close()
    assert "uvloop-ok" in out


# -- workspace_for chown 给 sandbox 用户 ----------------------------------

def test_workspace_for_chowns_to_sandbox_when_root(monkeypatch, tmp_path):
    monkeypatch.setenv("SOPHAGENT_DATA_DIR", str(tmp_path / "data"))
    from sophagent import config as config_mod
    import sophagent.config as cfgmod
    config_mod.reset_config()
    cfg = config_mod.get_config()
    monkeypatch.setattr(cfgmod, "exec_credentials", lambda: (1234, 5678))
    chowned = {}
    monkeypatch.setattr(cfgmod.os, "chown", lambda p, u, g: chowned.update(path=str(p), uid=u, gid=g))
    ws = cfg.workspace_for(7)
    config_mod.reset_config()
    assert chowned == {"path": str(ws), "uid": 1234, "gid": 5678}


def test_workspace_for_no_chown_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("SOPHAGENT_DATA_DIR", str(tmp_path / "data"))
    from sophagent import config as config_mod
    import sophagent.config as cfgmod
    config_mod.reset_config()
    cfg = config_mod.get_config()
    monkeypatch.setattr(cfgmod, "exec_credentials", lambda: None)
    called = {"n": 0}
    monkeypatch.setattr(cfgmod.os, "chown", lambda *a: called.update(n=called["n"] + 1))
    cfg.workspace_for(7)
    config_mod.reset_config()
    assert called["n"] == 0
