# 文件管理功能设计

日期：2026-06-17
分支：`feature-file-management`
状态：已通过设计评审，待实现

## 背景

sophclaw-agent 现有架构已具备文件管理的基础：

- **每用户工作目录** `data/workspaces/<user_id>`（`config.py: workspace_for()`），跨该用户所有会话共享。
- **文件工具** `read_file / write_file / edit_file / list_dir`（`tools/files.py`），全部经 `safe_path()` 收敛在工作目录内，拒绝 `..`、绝对路径、符号链接逃逸。
- **前端** 单文件原生 JS（`web/index.html`），Chat/Agents/Groups/Admin 标签页，底部 `#composer` 为输入框 + 发送。
- **消息** 为纯文本（`messages.content TEXT`），模型纯文本、无多模态。

参考 hermes-agent（`hermes_cli/web_server.py`）的 `/api/files/*` 一套 REST 接口（list / read / upload / mkdir / delete，操作一个「托管根目录」）。其「加入上下文」的本质是：文件上传进工作目录后，agent 通过自己的文件工具按需读取——文件与 agent 共享同一工作目录。该模型可 1:1 套到 sophclaw 现有的「每用户工作目录 + 文件工具」上。

## 需求

- **REQ2.1**：web 端右侧打开目录树，查看文件结构。
- **REQ2.2**：对话框左边添加上传文件的按钮，能把文件加入对话上下文。
- **REQ2.3**：暂定所有登录用户都拥有文件管理权限（后期再细化权限结构）。
- **REQ2.4**：参考 hermes-agent 实现「对话中添加文件到上下文」。

## 设计决策（已确认）

1. **加入上下文机制 = 引用式（hermes 同款）**：文件上传到用户工作目录，发送消息时在 content 前置一行 `[附加文件: <path>]`，agent 用 `read_file` 工具按需读取。省 token、支持大文件 / 二进制，后端 chat 接口零改动。
2. **目录树 = 只读**：浏览目录树、预览文件内容、下载。不在面板内做增删改（写操作由聊天 / 上传按钮完成）。
3. **上传落地位置 = 工作目录根**（与 hermes 一致、路径短）。
4. **缺 read_file 兜底**：若当前会话的 agent 未启用 `read_file` 工具，📎 按钮显示 tooltip 警告「当前 agent 未启用 read_file，附件可能无法被读取」。附件仍会上传成功。
5. **权限**：不引入新权限结构，所有 `require_user` 通过即可，作用域天然隔离在本人工作目录。代码注释标注「后期再细化权限」。

## 架构

新建 `sophclaw/api/file_routes.py`，挂载到 `/api/files`，操作 `cfg.workspace_for(user_id)`，复用 `tools/files.py: safe_path()` 做安全收敛。与 hermes 的 `/api/files/*` 对应，且与 `read_file/list_dir` 工具共享同一工作目录——上传的文件 agent 立刻能读到。

（替代方案：塞进 `session_routes.py`。否决——文件属用户级而非会话级，且会让会话路由臃肿。）

## 后端 API

新文件 `sophclaw/api/file_routes.py`，前缀 `/api/files`，在 `api/__init__.py: mount_routes()` 注册。全部 `Depends(require_user)`，作用域恒为调用者自己的工作目录。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/api/files` | query `path`（缺省 = 根） | `{path, parent, entries: [{name, path, is_dir, size, mtime}]}`，目录在前、按名排序 |
| GET | `/api/files/read` | query `path` | 文本：`{name, path, size, mime, content}`；二进制或超文本上限：`{name, path, size, mime, data_url}`（base64，供下载/预览） |
| POST | `/api/files/upload` | body `{path, data_url, overwrite?}` | `{ok, entry}`；data_url 为 `data:<mime>;base64,<...>` |

行为细节：

- **路径安全**：所有 `path` 经 `safe_path(workspace, path)`，越界抛 400/403。`path` 为相对工作目录的路径。
- **列目录**：`entries` 中 `is_dir` 为真的排在前，其余按名（小写）排序；`size`/`mtime` 来自 `stat()`，目录 `size` 为 null。`parent` 为相对父目录路径，根目录时为 null。
- **读文件**：先按 `stat()` 取大小，超 `max_upload_bytes`(10MB) 直接返回 413（避免巨大 base64）。否则：能以 UTF-8 解码且 ≤ `MAX_READ_BYTES`(256KB) 返回 `content`（文本预览）；其余（二进制或 256KB~10MB 的文本）返回 `data_url` 供前端下载 / 预览。
- **上传**：解码 data_url（仅接受 base64 data URL，否则 400），大小超 `max_upload_bytes` 返回 413。落地到工作目录根（`path` 即目标文件名）。名字冲突且 `overwrite` 非真时，自动改名为 `name (1).ext`、`name (2).ext`…。
- **不实现** mkdir / delete / rename（只读范围）。

配置新增（`config.py: Config`）：`max_upload_bytes: int = 10 * 1024 * 1024`（10MB），可由环境变量 `SOPHCLAW_MAX_UPLOAD_BYTES` 覆盖。

## 前端（`web/index.html`）

### 目录树面板（REQ2.1）

- `#chat` 改为横向 flex：左侧消息列（`#msgs` + `#composer`）不变，右侧新增可折叠面板 `#filetree`。
- composer 左侧加 📁 切换按钮，开关 `#filetree`。
- 面板内：调 `GET /api/files` 渲染目录树。目录可点击展开 / 收起（懒加载子目录）；文件点击 → 调 `GET /api/files/read`，文本在弹层 / 预览区展示，附「⬇下载」按钮（用 data_url 或 content 生成 Blob 下载）。

布局示意：

```
┌─ Chat ─────────────────────────────┬─ 📁 Files ──────┐
│  [消息气泡区 #msgs]                  │  ▸ src/         │
│                                     │    notes.txt    │
│  ┌───────────────────────────────┐  │    data.csv     │
│  │📎 📁 [输入框........] [Send]   │  │  (点击=预览)    │
└──┴───────────────────────────────┴──┴────────────────┘
```

### 上传按钮 + 加入上下文（REQ2.2，引用式）

- composer 左侧加 📎 按钮 → 触发隐藏的 `<input type=file>`。
- 选中文件后**立即**读为 base64 data_url，`POST /api/files/upload` 进工作目录根；成功后在输入框上方显示**附件 chip**（文件名 + ✕ 移除）。
- 点 Send 时，把每个 chip 的路径以 `[附加文件: <path>]` 形式逐行**前置进消息 content** 再发送（现有 `sendMsg()` 微调）。Agent 收到后用 `read_file(path)` 读取。后端 chat 接口零改动。
- 发送后清空 chips。
- **缺 read_file 兜底**：打开会话时已知该会话 agent；若其 `tools` 不含 `read_file`，给 📎 按钮加 `title`（tooltip）警告。需要会话详情里能拿到 agent 的 tools 列表（`openSession` 已取 `/api/sessions/{id}`，确认其是否含 agent tools；不含则附带取 agent 信息）。

## 安全与限制

- 所有路径经 `safe_path()`，复用现有逃逸防护。
- 上传大小上限 `max_upload_bytes`（默认 10MB），超限 413。
- 读文件文本上限沿用 `MAX_READ_BYTES`(256KB)；更大或二进制走 data_url。
- 引用式依赖 agent 启用 `read_file`；缺失时 UI 警告，但不阻断上传。

## 测试（`tests/test_files_api.py`）

沿用现有 fake-provider / `conftest.py` 风格：

- 上传 → 列目录 → 读取 往返（文本）。
- `..` / 绝对路径逃逸被拒（400/403）。
- 超 `max_upload_bytes` 上传返回 413。
- 名字冲突自动改名（`name (1).ext`）。
- 未登录访问返回 401。
- 读二进制 / 大文件走 data_url 分支。

## 不在本次范围

- 目录树内的增 / 删 / 改 / 重命名（仅只读）。
- 细化的文件管理权限结构（REQ2.3 暂定全员可用）。
- 多模态 / 图片直接入模型消息（引用式 + read_file 文本读取已满足当前需求）。
