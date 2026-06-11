# sophclaw-agent

轻量级多用户、多 agent 服务，带 hermes 式自进化 skill 系统。
单进程 asyncio + FastAPI + SQLite，约 4000 行 Python，8 个直接依赖，镜像 ~200MB。

## 特性

- **模型**：任意 OpenAI 兼容第三方 API（可配 `base_url`）+ Anthropic 原生 API
- **多 agent**：管理员可定义多个 agent（system prompt / 模型 / 工具白名单 / skill 白名单），用户开会话时选择
- **多用户**：JWT 登录，admin / user 两级角色；session、工作目录、记忆全部按用户隔离
- **自进化**：skill 索引注入 system prompt，agent 通过 `skill_manage` 工具在运行时自主创建 / 改进 / 删除 skill（SKILL.md + YAML frontmatter，与 hermes 格式兼容）
- **内置工具**：文件读写、终端、Python 执行、web 搜索 / 抓取、长期记忆、skill 管理、子 agent 委派（`delegate_task`，深度限 1）
- **接口**：REST API + SSE 流式聊天 + OpenAI 兼容 `/v1/chat/completions` + 单文件 Web 界面（零前端依赖）

## 快速开始（Docker）

```bash
cp .env.example .env        # 填 ADMIN_PASSWORD 和至少一个 provider 的 api_key
docker compose up -d --build
# 浏览器打开 http://localhost:8000 ，用 admin 登录
```

首次启动自动创建 admin 账号（密码取 `ADMIN_PASSWORD`；未设置则生成随机密码打印在日志里：`docker compose logs sophclaw`）。

### 上手流程

1. admin 登录 → Admin 页签 → 创建 agent（选 provider、填模型名、勾选工具）
2. Admin 页签 → 添加普通用户
3. 任意用户：New session → 选 agent → 聊天
4. agent 在对话中学到可复用流程时会自建 skill；admin 可在 Skills 卡片审查 / 删除

## 本地开发

```bash
uv venv --python 3.12 .venv && uv pip install -p .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest          # 45+ 测试，全部使用 fake provider，无需真实 API key
ADMIN_PASSWORD=dev .venv/bin/uvicorn sophclaw.main:app --reload
```

## Provider 配置

**推荐方式**：在数据目录放 `providers.yaml`（本地为 `./data/providers.yaml`，容器为 `/data/providers.yaml`）。
也可用环境变量 `SOPHCLAW_PROVIDERS` 内联 JSON，但注意：在 shell 里 `source .env` 会剥掉 JSON 的双引号导致解析失败——内联方式只适合 docker compose 的 `environment:`（不经过 shell）。

```yaml
providers:
  deepseek:                      # 任意名字，agent 定义里引用它
    api_mode: openai             # openai = 一切 OpenAI 兼容 API
    base_url: https://api.deepseek.com/v1
    api_key: sk-...              # 或 "$DEEPSEEK_API_KEY" 引用环境变量
    context_limit: 64000
  anthropic:
    api_mode: anthropic
    api_key: $ANTHROPIC_API_KEY
    context_limit: 200000
```

API key 只存在于配置 / 环境变量中，不入库。

## API 概览

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/api/auth/login` | 公开 | → `{token}` |
| GET/POST/PATCH/DELETE | `/api/users[/{id}]` | admin | 用户管理 |
| GET/POST/PUT/DELETE | `/api/agents[/{id}]` | 读:user 写:admin | agent 定义 |
| GET/POST | `/api/sessions` | user | 自己的会话 |
| POST | `/api/sessions/{id}/chat` | owner | SSE 流（text_delta / tool_call / tool_result / done / error） |
| POST | `/api/sessions/{id}/stop` | owner | 中断当前 turn |
| GET/PUT/DELETE | `/api/skills[/{name}]` | 读:user 写:admin | skill 库审查通道 |
| POST | `/v1/chat/completions` | Bearer JWT | OpenAI 兼容；`model` = agent 名；支持 `stream` |
| GET | `/v1/models` | Bearer JWT | 列出 agents |

OpenAI SDK 直连示例：

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="<JWT token>")
print(client.chat.completions.create(model="helper",
      messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)
```

## Skill 格式（hermes 兼容）

```
/data/skills/<name>/SKILL.md     # YAML frontmatter（name, description 必填）+ markdown 正文
/data/skills/<name>/references/  # 可选支持文件（另有 scripts/ templates/ assets/）
```

## 安全边界（务必阅读）

- **容器是唯一的硬隔离边界。** `terminal` / `python_exec` 在容器内以子进程运行（cwd 钉在用户工作目录、环境变量白名单、超时与输出限额），但容器内多用户之间是软隔离——恶意用户原则上可通过 shell 越过工作目录约束。多租户敏感场景请在 agent 定义中不勾选这两个工具，或按用户拆分容器。
- `web_fetch` 拒绝解析到内网 / 回环地址的目标（防 SSRF）。
- agent 自建 skill 会进入所有 agent 的 system prompt 索引，存在被诱导写入误导性指引的风险；admin 应定期在 Web 界面审查 Skills 列表。
- 删除用户会级联删除其会话、消息、记忆和工作目录。

## 架构

```
FastAPI (单进程 asyncio)
  └─ SessionManager  per-session 锁 + 全局 Semaphore(32)
       └─ AgentRunner  模型↔工具循环, 两层上下文压缩
            ├─ providers/   openai | anthropic 适配器（流式, 工具调用）
            └─ tools/       files terminal python web memory skills delegate
数据: /data/sophclaw.db (SQLite WAL) + /data/skills/ + /data/workspaces/<uid>/
```

每个 turn 从 DB 加载历史 → 运行 → 释放，无常驻 agent 实例；常驻内存极小。
SQLite WAL 支撑数十并发会话；更重负载时 `db.py` 收口了全部 SQL，可平移到 Postgres。
