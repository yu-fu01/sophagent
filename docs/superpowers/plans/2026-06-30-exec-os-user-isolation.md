# exec 子进程 OS 用户隔离(P0#2)实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 `terminal`/`python_exec` 子进程以低权 `sandbox` 用户运行，使 `data/` 下密钥按 OS 文件权限对其不可读，在任意内核上根除降级路径泄漏。

**架构：** 容器以 root 起动 → 启动时设好 `data/` 分权 → exec 子进程经 `create_subprocess_exec(user=, group=)` 降权 → 与既有 Landlock 叠加。非 root/无 sandbox 用户时探测失败自动退回现状（②③ 层兜底）。

**技术栈：** Python `pwd`/`os`、asyncio subprocess `user=/group=`（已验证透传生效）、Docker（`python:3.12-slim` base）、pytest。

**设计文档：** `docs/superpowers/specs/2026-06-30-exec-os-user-isolation-design.md`

---

## 文件结构

| 文件 | 职责 | 动作 |
|---|---|---|
| `sophagent/tools/sandbox.py` | 加 `exec_credentials()`（降权凭据探测）；`sandbox_status()` 扩展含 uid 层 | 修改 |
| `sophagent/tools/terminal.py` | `_run_subprocess` 用降权凭据传 `user=/group=` | 修改 |
| `sophagent/config.py` | `workspace_for` 新建 workspace 时 chown 给 sandbox | 修改 |
| `sophagent/main.py` | 启动时设 `data/` 分权 + 用新版状态串打日志 | 修改 |
| `Dockerfile` | 建 sandbox 用户、root 起动 | 修改 |
| `tests/test_sandbox.py` | `exec_credentials()`、`sandbox_status()` 两态单测 | 修改 |

设计原则：探测与策略集中在 `sandbox.py`（单一职责：决定"以谁、在什么限制下"跑 exec）；`terminal.py` 只消费凭据；权限落地在 `main.py`(启动)与 `config.py`(workspace 生命周期)。

---

## 任务 1：`exec_credentials()` 降权凭据探测

**文件：**
- 修改：`sophagent/tools/sandbox.py`（在 `landlock_available` 后、`exec_argv` 前插入）
- 测试：`tests/test_sandbox.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_sandbox.py` 末尾：

```python
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
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_sandbox.py -k exec_credentials -q`
预期：FAIL，`AttributeError: module 'sophagent.tools.sandbox' has no attribute 'exec_credentials'`（以及 `sb.pwd` 不存在）。

- [ ] **步骤 3：编写最少实现代码**

在 `sophagent/tools/sandbox.py` 顶部 import 区加 `import pwd`（紧跟 `import os`）。在 `landlock_available()` 之后插入：

```python
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
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_sandbox.py -k exec_credentials -q`
预期：PASS（4 passed）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/tools/sandbox.py tests/test_sandbox.py
git commit -m "feat(security): exec_credentials() 探测 exec 降权用户(root+sandbox 用户)"
```

---

## 任务 2：`sandbox_status()` 扩展为两层状态

**文件：**
- 修改：`sophagent/tools/sandbox.py:65-75`（`sandbox_status`）
- 测试：`tests/test_sandbox.py`

设计：状态串需同时反映 uid 隔离与 Landlock 两层。现有测试 `test_sandbox_status_active`/`_degraded` 断言 `"active"`/`"landlock"`/`"degraded"` 子串——新格式 `exec sandbox: uid-isolation=<...>, landlock=<active|degraded>` 仍满足这些断言（landlock 分支保留 `active`/`degraded` 词），无需改旧测试。

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_sandbox.py`：

```python
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
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_sandbox.py -k "sandbox_status" -q`
预期：新增 2 个 FAIL（断言 `uid-isolation=` 子串不存在）；旧 2 个仍 PASS。

- [ ] **步骤 3：编写最少实现代码**

替换 `sophagent/tools/sandbox.py` 的 `sandbox_status()` 为：

```python
def sandbox_status() -> str:
    """One-line, log-friendly summary of the exec sandbox's effective state.

    Logged once at startup so operators can confirm which protection layers are
    actually in force: uid isolation (OS-user, any kernel) and Landlock
    (kernel/seccomp dependent). Both degrade silently otherwise."""
    uid = "active" if exec_credentials() is not None else "unavailable"
    landlock = "active" if landlock_available() else "degraded"
    return f"exec sandbox: uid-isolation={uid}, landlock={landlock}"
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_sandbox.py -q`
预期：全 PASS（含旧的 status 测试与任务 1 的测试）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/tools/sandbox.py tests/test_sandbox.py
git commit -m "feat(security): sandbox_status() 同时上报 uid 隔离与 Landlock 两层状态"
```

---

## 任务 3：`_run_subprocess` 接入降权

**文件：**
- 修改：`sophagent/tools/terminal.py:27-34`（`_run_subprocess` 的 `create_subprocess_exec` 调用）、import 区
- 测试：`tests/test_sandbox.py`（行为级；非 root 下凭据为 None，验证不传 user= 时一切照旧）

注：真正的"以 sandbox 身份被拒"由任务 6 的 Docker 实测覆盖（本地非 root 无法 setuid）。本任务的单测确保**非 root 路径零行为变化**且降权参数在有凭据时被正确传入（用 monkeypatch 注入假凭据 + 捕获调用）。

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_sandbox.py`：

```python
async def test_run_subprocess_passes_user_group_when_credentials(ctx, monkeypatch):
    """有降权凭据时，create_subprocess_exec 收到 user=/group=。"""
    import sophagent.tools.terminal as term
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["user"] = kwargs.get("user")
        captured["group"] = kwargs.get("group")
        raise RuntimeError("stop-after-capture")  # 不真正起进程

    monkeypatch.setattr(term, "exec_credentials", lambda: (4321, 8765))
    monkeypatch.setattr(term.asyncio, "create_subprocess_exec", fake_exec)
    try:
        await term._run_subprocess("echo hi", str(ctx.workspace), 5)
    except RuntimeError:
        pass
    assert captured["user"] == 4321 and captured["group"] == 8765


async def test_run_subprocess_omits_user_group_when_no_credentials(ctx, monkeypatch):
    """无凭据（非 root）时不传 user=/group=，保持原行为。"""
    import sophagent.tools.terminal as term
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["has_user"] = "user" in kwargs
        captured["has_group"] = "group" in kwargs
        raise RuntimeError("stop")

    monkeypatch.setattr(term, "exec_credentials", lambda: None)
    monkeypatch.setattr(term.asyncio, "create_subprocess_exec", fake_exec)
    try:
        await term._run_subprocess("echo hi", str(ctx.workspace), 5)
    except RuntimeError:
        pass
    assert captured["has_user"] is False and captured["has_group"] is False
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_sandbox.py -k run_subprocess -q`
预期：FAIL（`terminal` 模块无 `exec_credentials` 名字，monkeypatch 报 AttributeError）。

- [ ] **步骤 3：编写最少实现代码**

`sophagent/tools/terminal.py` import 区改为同时引入 `exec_credentials`：

```python
from .sandbox import exec_argv, exec_credentials
```

替换 `_run_subprocess` 的进程创建块（当前 27-34 行）为：

```python
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
```

（其余 try/wait_for/kill 逻辑不变。）

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_sandbox.py -k run_subprocess -q`
预期：PASS（2 passed）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/tools/terminal.py tests/test_sandbox.py
git commit -m "feat(security): _run_subprocess 在有凭据时 setuid 降权到 sandbox 用户"
```

---

## 任务 4：`workspace_for` 新建 workspace 时 chown 给 sandbox

**文件：**
- 修改：`sophagent/config.py:87-90`（`workspace_for`）
- 测试：`tests/test_sandbox.py`

设计：降权后的子进程以 sandbox 身份需读写自己的 workspace。主进程为 root 时把新建 workspace chown 给 sandbox。非 root（无凭据）时 chown 会失败——静默忽略（此时本就不降权，无影响）。

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_sandbox.py`：

```python
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
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run --extra dev pytest tests/test_sandbox.py -k workspace_for -q`
预期：FAIL（`config` 模块无 `exec_credentials`；chown 未被调用）。

- [ ] **步骤 3：编写最少实现代码**

`sophagent/config.py` import 区加：

```python
from .tools.sandbox import exec_credentials
```

> 注意循环导入风险：`tools/sandbox.py` 仅 import 标准库（ctypes/os/sys/pwd/pathlib），不 import config，故 `config → sandbox` 单向安全。若运行时报循环导入，改为函数内延迟 import。

替换 `workspace_for`：

```python
    def workspace_for(self, user_id: int) -> Path:
        ws = self.workspaces_dir / str(user_id)
        existed = ws.exists()
        ws.mkdir(parents=True, exist_ok=True)
        if not existed:
            creds = exec_credentials()
            if creds is not None:
                try:
                    os.chown(ws, creds[0], creds[1])
                except OSError:
                    pass  # 非 root 或权限不足：不降权场景，忽略
        return ws
```

- [ ] **步骤 4：运行测试验证通过**

运行：`uv run --extra dev pytest tests/test_sandbox.py -k workspace_for -q`
预期：PASS（2 passed）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/config.py tests/test_sandbox.py
git commit -m "feat(security): workspace_for root 下 chown 新 workspace 给 sandbox 用户"
```

---

## 任务 5：启动时设 `data/` 分权 + 更新日志

**文件：**
- 修改：`sophagent/main.py:54-56` 区域（`load_all()` 后）
- 无新单测（权限落地由任务 6 Docker 实测覆盖；逻辑用 root 守卫，非 root 无副作用）

设计：主进程为 root 时，启动设好密钥文件 `0600`、`data/` 与 `workspaces/` `0711`、`skills/` 可读。非 root 跳过。日志已由任务 2 的新 `sandbox_status()` 自动反映两层。

- [ ] **步骤 1：实现权限设置 + 日志**

在 `sophagent/main.py` 的 `lifespan` 中，把现有

```python
    load_all()
    from .tools.sandbox import sandbox_status
    status = sandbox_status()
    (log.info if "active" in status else log.warning)(status)
    app.state.db = db
```

替换为：

```python
    load_all()
    _harden_data_dir(cfg)
    from .tools.sandbox import sandbox_status, exec_credentials
    status = sandbox_status()
    # 容器内(root)但拿不到降权凭据 = sandbox 用户缺失，属配置异常 → 告警
    misconfig = __import__("os").geteuid() == 0 and exec_credentials() is None
    (log.warning if ("degraded" in status or misconfig) else log.info)(status)
    app.state.db = db
```

在 `main.py` 模块级（`_bootstrap_admin` 之前）加 `import os` 已存在则复用，并新增辅助函数：

```python
def _harden_data_dir(cfg) -> None:
    """root 容器内收紧 data 目录权限：密钥文件 0600，目录 0711(可穿行不可列)，
    使降权后的 exec 子进程(sandbox 用户)读不到密钥但能进入自己的 workspace。
    非 root 环境直接跳过(本地开发无降权)。"""
    if os.geteuid() != 0:
        return
    import stat
    for d in (cfg.data_dir, cfg.workspaces_dir, cfg.skills_dir):
        try:
            os.chmod(d, 0o711)
        except OSError:
            pass
    cfg.skills_dir.chmod(0o755) if cfg.skills_dir.exists() else None  # 脚本需可读
    for f in (cfg.db_path, cfg.data_dir / ".secret", cfg.data_dir / "providers.yaml"):
        try:
            if f.exists():
                os.chmod(f, 0o600)
        except OSError:
            pass
```

> `import os` 已在 main.py？当前未导入。在 import 区（`import secrets` 附近）加 `import os`，并把上面 `__import__("os")` 改回 `os`。

- [ ] **步骤 2：运行全量测试验证无回归**

运行：`uv run --extra dev pytest -q`
预期：全 PASS（非 root 下 `_harden_data_dir` 直接 return，行为不变）。

- [ ] **步骤 3：本地启动冒烟验证日志**

运行：
```bash
SOPHAGENT_DATA_DIR=/tmp/_p0_2 ADMIN_PASSWORD=x uv run python -c "
import asyncio
from sophagent.main import lifespan, create_app
async def m():
    async with lifespan(create_app()):
        pass
asyncio.run(m())
" 2>&1 | grep "exec sandbox"
rm -rf /tmp/_p0_2
```
预期：`exec sandbox: uid-isolation=unavailable, landlock=active`（本地非 root + 本机有 Landlock）。

- [ ] **步骤 4：Commit**

```bash
git add sophagent/main.py
git commit -m "feat(security): 启动时收紧 data 目录权限(root) + 日志上报两层沙箱状态"
```

---

## 任务 6：Dockerfile 改造 + 容器内权威实测

**文件：**
- 修改：`Dockerfile`
- 验证：容器内攻击自测脚本（临时，不入库）

- [ ] **步骤 1：改 Dockerfile**

将 `sophagent/Dockerfile` 改为（建 sandbox 用户、保持 root 起动，去掉 `USER app`）：

```dockerfile
FROM python:3.12-slim

RUN useradd -m -u 1000 app \
 && useradd -u 1001 -M -s /usr/sbin/nologin sandbox

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY sophagent/ sophagent/
COPY web/ web/
RUN pip install --no-cache-dir .

# /data 由 root 持有；启动时 _harden_data_dir 收紧到 0711 + 密钥 0600，
# workspace 由 workspace_for chown 给 sandbox。
RUN mkdir -p /data

# 以 root 起动：主进程持有密钥，exec 子进程经 user= 降权到无权读 /data 的 sandbox。
ENV SOPHAGENT_DATA_DIR=/data
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["uvicorn", "sophagent.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **步骤 2：构建镜像**

运行：`docker build -t sophagent-p0-2-test .`
预期：构建成功。

- [ ] **步骤 3：容器内权威攻击自测**

写临时脚本 `/tmp/p0_2_probe.py`（直接走真实 dispatch 路径，需 root 容器 + sandbox 用户）：

```python
import asyncio, os, importlib.util
# 直接加载 sandbox.py + terminal 需要的最小路径，或用完整 dispatch
import sys; sys.path.insert(0, "/app")
from sophagent.tools.sandbox import exec_credentials, sandbox_status
from sophagent.tools import terminal as term

print("euid:", os.geteuid(), "| status:", sandbox_status(), "| creds:", exec_credentials())

# 模拟生产布局：secret 在 /data 根(0600 root)，workspace 归 sandbox
os.makedirs("/data/workspaces/1", exist_ok=True)
SECRET = "P0-2-DOCKER-SECRET-DO-NOT-LEAK"
open("/data/providers.yaml", "w").write("api_key: " + SECRET + "\n")
os.chmod("/data", 0o711); os.chmod("/data/providers.yaml", 0o600)
import pwd; sb = pwd.getpwnam("sandbox")
os.chown("/data/workspaces/1", sb.pw_uid, sb.pw_gid)
open("/data/workspaces/1/mine.txt", "w").write("ws-ok\n")
os.chmod("/data/workspaces/1/mine.txt", 0o644)

async def run(cmd):
    return await term._run_subprocess(cmd, "/data/workspaces/1", 10)

async def main():
    out = await run("cat ../../providers.yaml")
    print("攻击读密钥:", "❌ LEAK" if SECRET in out else "✅ 被挡", "|", out.strip()[:60])
    out = await run("cat ../../.secret 2>&1 || true; id")
    print("身份+读.secret:", out.strip()[:80])
    out = await run("cat mine.txt")
    print("读自身 workspace:", "✅ 正常" if "ws-ok" in out else "❌ 误伤", "|", out.strip()[:40])
asyncio.run(main())
```

运行：
```bash
docker run --rm -v /tmp/p0_2_probe.py:/probe.py:ro sophagent-p0-2-test python /probe.py
```
预期：
- `euid: 0 | status: exec sandbox: uid-isolation=active, landlock=active | creds: (1001, 1001)`
- 攻击读密钥：✅ 被挡（Permission denied）
- 身份：`uid=1001(sandbox)` —— 证明确实降权
- 读自身 workspace：✅ 正常

清理：`rm -f /tmp/p0_2_probe.py`

- [ ] **步骤 4：验证降级路径（模拟无 Landlock 仍被 uid 挡）**

在上面 probe 里临时 `import sophagent.tools.sandbox as sb; sb.landlock_available=lambda:False` 后重跑 `run("cat ../../providers.yaml")`，预期仍 `✅ 被挡`（证明 uid 层独立于 Landlock 生效——这是 P0#2 的核心价值）。

- [ ] **步骤 5：Commit**

```bash
git add Dockerfile
git commit -m "feat(security): Dockerfile 建 sandbox 用户、root 起动以支持 exec 降权"
```

---

## 任务 7：全量回归 + 收尾

- [ ] **步骤 1：全量测试**

运行：`uv run --extra dev pytest -q`
预期：全 PASS（≥325 + 新增）。

- [ ] **步骤 2：更新设计文档状态**

把 `docs/superpowers/specs/2026-06-30-exec-os-user-isolation-design.md` 的 `状态:` 改为 `已实现`。

- [ ] **步骤 3：Commit**

```bash
git add docs/superpowers/specs/2026-06-30-exec-os-user-isolation-design.md
git commit -m "docs(spec): P0#2 标记为已实现"
```

---

## 自检结果

**规格覆盖度：** 文件权限分治→任务 5+6；降权机制→任务 3+6；检测配置→任务 1；启动日志→任务 2+5；workspace 归属→任务 4；三层降级→任务 1/2/3 协同；测试(单测+Docker)→任务 1-4 单测、任务 6 Docker。全覆盖。

**占位符扫描：** 无 TODO/待定；每个代码步骤含完整代码与精确命令/预期。

**类型一致性：** `exec_credentials() -> (uid,gid)|None` 在任务 1 定义，任务 2/3/4 一致消费；`sandbox_status()` 新格式与任务 2 测试断言一致；`_harden_data_dir`/`workspace_for` 签名前后一致。
