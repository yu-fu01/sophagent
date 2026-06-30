# exec 子进程 OS 用户隔离(P0#2)设计

日期:2026-06-30
状态:已实现并经 Docker 实测（2026-06-30）。含三轮纠错：补充组泄漏(extra_groups)、uvloop 不兼容(降权移入启动器)、P3 启动期功能自检。详见末尾「验证过程与纠错记录」。
关联:`37af5b5`(Landlock 沙箱 + 明文脱敏)、`729e0e4`(沙箱状态日志);参考漏洞报告 ai-cs-qa API Key 提取

## 背景与目标

`terminal` / `python_exec` 工具在容器内以**与主进程同一 OS 用户**运行子进程,对整个文件系统有读权。当 Landlock 不可用(内核 <5.13 或 Docker seccomp 拦截)时,降级路径下一个攻击者投递的子进程可读到 `data/` 下的全部敏感文件:

| 文件 | 泄漏后果 |
|---|---|
| `data/providers.yaml` | 明文上游 provider API key |
| `data/.secret` | 签 JWT 的密钥 → 伪造任意用户登录态;且可解密 DB 内加密的 provider key |
| `data/sophagent.db` | `users.password_hash`(离线爆破)、`providers.api_key_enc`(配 `.secret` 可解) |

加密、改环境变量、挪文件位置对"同 OS 用户的子进程"都只是抬高门槛(同用户能读能组合),挡不住。**唯一能在任意内核上根除的办法:让 exec 子进程以一个无权读 `data/` 的低权 OS 用户运行。**

目标:exec 子进程以专用低权用户 `sandbox` 运行,`data/` 下密钥对它不可读,而其自身 workspace 可读写;与既有 Landlock 叠加为两条独立防线;不可用时优雅降级。

非目标:不改密钥的存储格式(仍 yaml 明文 + DB Fernet);不收紧多用户之间的 workspace 软隔离边界(README 已声明软隔离)。

## 架构:三层独立降级

```
① uid 隔离   —— root 起动 + sandbox 用户存在 → 任意内核生效(OS 文件权限)
② Landlock   —— 内核/seccomp 支持        → 任意用户生效(已实现)
③ 明文脱敏   —— 永远兜底(已实现)
```

三层互相独立,能力满足的层自动叠加。本设计新增第 ① 层。

## 组件设计

### 1. 文件权限分治模型

容器内新建低权用户 `sandbox`(uid 1001,无登录、独立同名组)。`data/` 按"可穿行、密钥不可读、workspace 可读写"分权:

| 路径 | 权限 | 属主 | sandbox 效果 |
|---|---|---|---|
| `/data` | `0711` | root | 可穿行,不可列目录 |
| `/data/providers.yaml`、`/data/.secret`、`/data/sophagent.db*` | `0600` | root | ❌ 读不到 |
| `/data/skills/` | `0755` | root | ✅ 可读(跑 skill 脚本需要) |
| `/data/workspaces/` | `0711` | root | 可穿行 |
| `/data/workspaces/<uid>/` | `0700` | **sandbox** | ✅ 读写自己的 workspace |

关键:`/data` 给 `o+x`(穿行)而不给 `o+r`(列举),密钥文件 `0600`——sandbox 穿过 `/data` 仍读不到密钥。secret 与 workspace 共处一树也能干净分权。

### 2. 降权机制

- **容器以 root 起动**:Dockerfile 去掉 `USER app`(主进程持有全部密钥,其权限边界本就是"全部";真正的 RCE 面 exec 已降到最低权)。
- **启动时一次性设权**:lifespan 中(主进程为 root)对 `data/` 与密钥文件 `chmod/chown` 为上表权限,幂等。
- **exec 降权**:`terminal._run_subprocess` 用 Python 原生 `create_subprocess_exec(..., user=sandbox_uid, group=sandbox_gid)` 降权(无需手写 preexec_fn,Python ≥3.9 的 Popen kwargs,asyncio 透传)。
- **与 Landlock 叠加**:降权后子进程仍经现有 `exec_argv()` 的 Landlock 启动器(非 root 下 Landlock 自限制已验证可用)。执行顺序:内核按 `user=` 降权 → 启动器以 sandbox 身份加 Landlock → execvp 真实命令。
- **workspace 归属**:`config.workspace_for(user_id)` 新建 workspace 时 `chown` 给 sandbox(主进程为 root 有权),使降权后的子进程可读写。

### 3. 检测与配置

- `sandbox.exec_credentials() -> tuple[int,int] | None`:仅当 `os.geteuid()==0` 且目标用户存在(`pwd.getpwnam`)时返回 `(uid,gid)`,否则 `None`。
- 用户名可由环境变量 `SOPHAGENT_EXEC_USER` 配置,默认 `sandbox`。
- 本地裸跑(非 root / 无 sandbox 用户):返回 `None` → 不传 `user=` → 退回当前行为(不崩),由 ②③ 层兜底。

### 4. 启动日志(扩展既有)

`sandbox_status()` 输出扩展为同时反映两层:

```
exec sandbox: uid-isolation=active, landlock=active
exec sandbox: uid-isolation=unavailable, landlock=active     # 本地裸跑
exec sandbox: uid-isolation=active, landlock=degraded         # 老内核 Docker
```

`uid-isolation=unavailable` 在容器(root)环境属配置异常 → 走 `log.warning`;本地裸跑属预期 → 由调用处区分(容器内 euid==0 但无 sandbox 用户才告警)。

## 数据流(一次 exec 调用)

1. agent 触发 `python_exec`/`terminal` → `_run_subprocess(cmd, workspace, timeout)`。
2. `exec_credentials()` 返回 `(uid,gid)` 或 `None`。
3. `exec_argv(cmd, workspace)` 产出 argv(含/不含 Landlock 启动器)。
4. `create_subprocess_exec(*argv, cwd=workspace, env=_safe_env(), user=uid, group=gid, start_new_session=True)`;`None` 时省去 `user/group`。
5. 内核降权 → (Landlock 自限制) → 执行;读 `data/` 密钥被 OS 权限 `EACCES` 拦截。
6. stdout/stderr 截断回收 → 经 `redact_known_secrets` 兜底 → 回写 history/UI。

## 错误处理

- 运行期 setuid 失败(权限被改等):子进程启动抛错,沿用现有"返回错误文本"路径,不影响其他工具。
- root 容器内 sandbox 用户缺失(镜像构建/配置错):`exec_credentials()` 返 `None` → 降级到 ②③ 并 `warning` 大声报。
- workspace `chown` 失败(非 root):忽略并继续(此时 `exec_credentials` 本就返 `None`,无影响)。

## 测试

- **单测**:`exec_credentials()` 检测分支(monkeypatch `os.geteuid`/`pwd.getpwnam`:root+用户在→返凭据;非 root→None;root 无用户→None);`sandbox_status()` 串含 `uid-isolation=` 两态。
- **既有**:325 测试保持绿;`terminal`/`python_exec` 既有用例在本地(非 root,`exec_credentials`→None)行为不变。
- **权威验证(Docker)**:build 镜像、起容器,从 workspace 外发起 `python_exec`/`terminal` 攻击(读 `../../providers.yaml`、`.secret`、解 DB)→ 以 sandbox 身份 `Permission denied`;workspace 内文件读写、`uv run` 跑 skill 正常;校验启动日志 `uid-isolation=active`。与验证 Landlock 同法。

## 影响与回滚

- 改动文件:`Dockerfile`、`sophagent/tools/sandbox.py`、`sophagent/tools/_sandbox_exec.py`(启动器:降权 + Landlock)、`sophagent/tools/terminal.py`(走启动器 + 启动自检)、`sophagent/config.py`(workspace chown)、`sophagent/main.py`(设权 + 日志 + 启动自检)、`tests/test_sandbox.py`、README。
- 行为变化:仅在 root 容器 + sandbox 用户存在时启用降权;其余环境零行为变化。
- 回滚:恢复 Dockerfile `USER app`,`exec_credentials()` 在非 root 下自然返 `None`,代码路径自动退回。

## 验证过程与纠错记录（诚实留痕，供审计）

实现后的 Docker 实测暴露了两个单测/本地探针都没抓到的真实缺陷，记录如下：

### 纠错 1：降权子进程残留 root 补充组（已修）
- **现象**：Docker 内 `id` 显示降权后仍 `groups=…,0(root)`。
- **根因**：`create_subprocess_exec(user=,group=)` 不调用 `setgroups()`，子进程继承 root 父进程的补充组（gid 0）。降级路径（无 Landlock）下，任何 `group-root` 可读文件（如 0640）仍可被读。
- **修复**：丢弃继承的补充组。最终随纠错 2 一并落到启动器里的 `setgroups([])`。

### 纠错 2：uvloop 拒绝 user/group/extra_groups，生产 exec 全崩（已修，关键）
- **现象**：在真实 `uvicorn`（用 **uvloop** 事件循环）部署里，uid 隔离一旦激活，`terminal`/`python_exec` 直接 `ValueError: unexpected kwargs: user, group, extra_groups`——连合法使用都坏；而 `sandbox_status()` 仍报 `active`。
- **根因**：privilege drop 经由 `create_subprocess_exec` 的 `user/group/extra_groups` kwargs 实现，标准 `asyncio` 支持、**uvloop 不支持**。pytest 与早期 Docker 探针都用普通 `asyncio.run()`，从未走 uvloop 路径 → 漏测。
- **修复**（commit `235e997`）：把降权（`setgroups([])`/`setgid`/`setuid`）与 Landlock 一并移入启动器进程 `_sandbox_exec.py`，`_run_subprocess` 不再向 `create_subprocess_exec` 传任何 loop 不兼容 kwargs，事件循环无关。新增回归：uvloop 循环下 `_run_subprocess` 正常执行 + 绝不传 loop 不兼容 kwargs。
- **教训**：单测的事件循环（asyncio）≠ 生产事件循环（uvloop）。**安全关键路径必须在与生产同构的环境（同镜像、同 loop、root + sandbox 用户）下实测**，不能只靠单测。

### 加固：启动期功能自检（P3，commit `a1e899f`）
- **动机**：上面 uvloop bug 期间 `sandbox_status()` 照报 `active`——配置级检查查不出功能性退化。
- **做法**：`selftest_exec` 在启动时（生产 uvloop 循环下）真的走启动器跑一条命令，断言 (a) exec 能执行、(b) 隔离应生效时读不到沙箱外 canary 文件。隔离本应生效却自检失败 → **默认拒绝启动**（`SOPHAGENT_SANDBOX_SELFTEST=warn/off` 可降级为告警）。
- **实测**：真实 uvicorn 启动打印「自检通过」；注入 exec 崩溃（模拟本 bug 复发）→ 自检捕获并拒绝启动。这一关能把同类回归挡在上线前。

### 最终验证状态（Docker，同生产镜像 + uvloop + root/sandbox）
- 三类密钥（`providers.yaml` / `.secret` / `sophagent.db*`）在 **Landlock+uid** 与 **仅 uid（降级）** 两路径下均 `Permission denied`；
- 合法 `python_exec`（含 umask 0077）正常输出；exec 身份 `uid=1001(sandbox) groups=1001(sandbox)`（无 root 组）；
- 启动日志 `uid-isolation=active, landlock=active` + `exec 自检通过`。

