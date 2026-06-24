# 文件管理功能 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 给 sophagent web 端加只读目录树 + 对话框上传按钮，上传文件落入用户工作目录并以引用形式加入对话上下文，agent 用 `read_file` 读取。

**架构：** 新增 `sophagent/api/file_routes.py`（`/api/files` 的 list/read/upload），作用域恒为调用者的 `workspace_for(user_id)`，复用 `tools.files.safe_path()` 收敛路径。前端 `web/index.html` 加右侧可折叠目录树面板与 composer 左侧 📎/📁 按钮；上传后显示附件 chip，发送时把 `[附加文件: <path>]` 前置进消息。后端 chat 接口零改动。

**技术栈：** FastAPI + pydantic、aiosqlite、pytest + TestClient（fake provider）、单文件原生 JS 前端。

**规格：** `docs/superpowers/specs/2026-06-17-file-management-design.md`

---

## 文件结构

- **创建** `sophagent/api/file_routes.py` — `/api/files` 三个端点（list/read/upload），仅依赖 `require_user` + `safe_path`，无新权限。
- **创建** `tests/test_files_api.py` — 端到端测试，沿用 `conftest.py` 的 `client`/`bob` 夹具。
- **修改** `sophagent/config.py` — `Config` 加 `max_upload_bytes` 字段 + `load_config()` 读环境变量。
- **修改** `sophagent/api/__init__.py` — 注册 `file_routes` 到 `/api/files`。
- **修改** `web/index.html` — 目录树面板（REQ2.1）、上传按钮 + 附件 chip + 引用前置 + tooltip（REQ2.2）。
- **修改** `README.md` — 特性列表补一句文件管理。

---

## 任务 1：config 增加上传上限

**文件：**
- 修改：`sophagent/config.py`（`Config` 数据类 + `load_config()`）

- [ ] **步骤 1：给 `Config` 加字段**

在 `sophagent/config.py` 的 `Config` 数据类里，`tool_output_limit` 一行之后加：

```python
    max_upload_bytes: int = 10 * 1024 * 1024  # 上传/下载单文件上限 (10MB)
```

- [ ] **步骤 2：`load_config()` 读环境变量**

在 `load_config()` 构造 `Config(...)` 的关键字参数里（`max_concurrent_turns=...` 之后）加一行：

```python
        max_upload_bytes=int(os.environ.get("SOPHAGENT_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))),
```

- [ ] **步骤 3：验证导入无误**

运行：`.venv/bin/python -c "from sophagent.config import load_config"`
预期：无报错（无输出）。

- [ ] **步骤 4：Commit**

```bash
git add sophagent/config.py
git commit -m "feat(M-files): config 增加 max_upload_bytes (默认 10MB)"
```

---

## 任务 2：后端 `/api/files` 路由（TDD）

**文件：**
- 创建：`sophagent/api/file_routes.py`
- 创建：`tests/test_files_api.py`
- 修改：`sophagent/api/__init__.py`

- [ ] **步骤 1：编写失败的测试**

创建 `tests/test_files_api.py`：

```python
"""文件管理 API 端到端测试（/api/files），复用 conftest 的 client/bob 夹具。"""

import base64

from sophagent.config import get_config


def data_url(content: bytes, mime: str = "text/plain") -> str:
    return f"data:{mime};base64," + base64.b64encode(content).decode()


def test_upload_list_read_roundtrip(client, bob):
    # 上传一个文本文件
    resp = client.post("/api/files/upload",
                       json={"path": "notes.txt", "data_url": data_url(b"hello world")},
                       headers=bob)
    assert resp.status_code == 200, resp.text
    assert resp.json()["entry"]["name"] == "notes.txt"

    # 列根目录能看到它
    listing = client.get("/api/files", headers=bob).json()
    names = [e["name"] for e in listing["entries"]]
    assert "notes.txt" in names
    assert listing["parent"] is None

    # 读回内容
    read = client.get("/api/files/read", params={"path": "notes.txt"}, headers=bob).json()
    assert read["content"] == "hello world"
    assert read["size"] == 11


def test_path_escape_rejected(client, bob):
    # 列目录时 .. 逃逸被拒
    assert client.get("/api/files", params={"path": "../"}, headers=bob).status_code == 400
    # 读绝对路径被拒
    assert client.get("/api/files/read", params={"path": "/etc/passwd"}, headers=bob).status_code == 400


def test_upload_too_large(client, bob):
    get_config().max_upload_bytes = 8  # 临时调低上限
    resp = client.post("/api/files/upload",
                       json={"path": "big.txt", "data_url": data_url(b"123456789")},
                       headers=bob)
    assert resp.status_code == 413


def test_upload_dedupe(client, bob):
    body = {"path": "dup.txt", "data_url": data_url(b"a")}
    first = client.post("/api/files/upload", json=body, headers=bob).json()
    second = client.post("/api/files/upload", json=body, headers=bob).json()
    assert first["entry"]["name"] == "dup.txt"
    assert second["entry"]["name"] == "dup (1).txt"


def test_read_binary_returns_data_url(client, bob):
    payload = bytes([0, 1, 2, 255, 254])
    client.post("/api/files/upload",
                json={"path": "blob.bin", "data_url": data_url(payload, "application/octet-stream")},
                headers=bob)
    read = client.get("/api/files/read", params={"path": "blob.bin"}, headers=bob).json()
    assert "content" not in read
    assert read["data_url"].startswith("data:application/octet-stream;base64,")


def test_unauthenticated(client):
    assert client.get("/api/files").status_code == 401
    assert client.post("/api/files/upload", json={"path": "x", "data_url": data_url(b"x")}).status_code == 401
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/test_files_api.py -q`
预期：FAIL/ERROR（`/api/files` 端点不存在，返回 404 或路由未注册）。

- [ ] **步骤 3：创建 `sophagent/api/file_routes.py`**

```python
"""文件管理 REST API，作用域恒为调用者的每用户工作目录。

REQ2.1（目录树/查看）、REQ2.2（上传→引用进对话）、REQ2.3（所有登录用户可用，
细化权限后期再做）。对标 hermes-agent 的 /api/files/*。所有路径经
tools.files.safe_path() 收敛在工作目录内。"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_user
from ..config import get_config
from ..tools.files import MAX_READ_BYTES, safe_path

router = APIRouter()


def _root(user) -> Path:
    return get_config().workspace_for(user["id"]).resolve()


def _safe(root: Path, p: str) -> Path:
    """safe_path 但把越界的 ValueError 转成 400。"""
    try:
        return safe_path(root, p)
    except ValueError:
        raise HTTPException(400, "invalid path")


def _entry(root: Path, p: Path) -> dict:
    st = p.stat()
    is_dir = p.is_dir()
    return {
        "name": p.name,
        "path": str(p.relative_to(root)),
        "is_dir": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
    }


class UploadBody(BaseModel):
    path: str
    data_url: str
    overwrite: bool = False


def _decode_data_url(data_url: str, limit: int) -> bytes:
    text = (data_url or "").strip()
    if not text.startswith("data:") or "," not in text:
        raise HTTPException(400, "payload must be a data URL")
    header, encoded = text.split(",", 1)
    if ";base64" not in header:
        raise HTTPException(400, "payload must be base64 encoded")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "payload is not valid base64")
    if len(data) > limit:
        raise HTTPException(413, "file too large")
    return data


def _dedupe(target: Path) -> Path:
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    i = 1
    while True:
        cand = target.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


@router.get("")
async def list_files(request: Request, path: str = "", user=Depends(require_user)):
    root = _root(user)
    target = _safe(root, path or ".")
    if not target.exists():
        raise HTTPException(404, "path not found")
    if not target.is_dir():
        raise HTTPException(400, "path is not a directory")
    entries = sorted(
        (_entry(root, c) for c in target.iterdir()),
        key=lambda e: (not e["is_dir"], e["name"].lower()),
    )
    rel = "" if target == root else str(target.relative_to(root))
    parent = None if target == root else str(target.parent.relative_to(root))
    return {"path": rel, "parent": parent, "entries": entries}


@router.get("/read")
async def read_file_api(request: Request, path: str, user=Depends(require_user)):
    root = _root(user)
    target = _safe(root, path)
    if not target.is_file():
        raise HTTPException(404, "file not found")
    cfg = get_config()
    size = target.stat().st_size
    if size > cfg.max_upload_bytes:
        raise HTTPException(413, "file too large")
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    data = target.read_bytes()
    if size <= MAX_READ_BYTES:
        try:
            return {"name": target.name, "path": path, "size": size, "mime": mime,
                    "content": data.decode("utf-8")}
        except UnicodeDecodeError:
            pass
    encoded = base64.b64encode(data).decode("ascii")
    return {"name": target.name, "path": path, "size": size, "mime": mime,
            "data_url": f"data:{mime};base64,{encoded}"}


@router.post("/upload")
async def upload_file_api(body: UploadBody, request: Request, user=Depends(require_user)):
    root = _root(user)
    cfg = get_config()
    data = _decode_data_url(body.data_url, cfg.max_upload_bytes)
    name = Path(body.path).name  # 只取文件名：上传一律落工作目录根
    if not name:
        raise HTTPException(400, "invalid filename")
    target = _safe(root, name)
    if target.exists() and target.is_dir():
        raise HTTPException(409, "a directory already exists at that path")
    if target.exists() and not body.overwrite:
        target = _dedupe(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"ok": True, "entry": _entry(root, target)}
```

- [ ] **步骤 4：注册路由**

修改 `sophagent/api/__init__.py`：import 列表里加 `file_routes`，并在 `mount_routes()` 里加一行（放在 `session_routes` 之后）：

```python
    app.include_router(file_routes.router, prefix="/api/files", tags=["files"])
```

import 块改为（按字母序插入 `file_routes`）：

```python
from . import (
    agent_routes,
    auth_routes,
    file_routes,
    group_routes,
    membership_routes,
    openai_compat,
    session_routes,
    skill_routes,
    user_routes,
)
```

- [ ] **步骤 5：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/test_files_api.py -q`
预期：6 passed。

- [ ] **步骤 6：跑全量测试无回归**

运行：`.venv/bin/python -m pytest -q`
预期：原有 45+ 测试 + 新增 6 个全部 PASS。

- [ ] **步骤 7：Commit**

```bash
git add sophagent/api/file_routes.py sophagent/api/__init__.py tests/test_files_api.py
git commit -m "feat(M-files): /api/files 列目录/读/上传 (REQ2.1-2.3)"
```

---

## 任务 3：前端目录树面板（REQ2.1）

> 本项目无 JS 测试框架，前端任务为"实现 + 跑服务手动验证"。每步代码精确到位。

**文件：**
- 修改：`web/index.html`（CSS、`#chat` 结构、JS）

- [ ] **步骤 1：CSS 加面板与树样式**

在 `<style>` 内 `#composer{...}` 那行之后插入：

```css
#chat{flex-direction:row}
#chatmain{flex:1;display:flex;flex-direction:column;min-width:0}
#filetree{width:280px;border-left:1px solid var(--border);background:var(--panel);display:flex;flex-direction:column;overflow-y:auto}
#filetree header{padding:10px 12px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px}
#filetree header b{flex:1}
#filetree .body{padding:6px 8px;overflow-y:auto}
.tnode{padding:2px 4px;border-radius:4px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.tnode:hover{background:var(--panel2)}
.tnode .sz{color:var(--dim);font-size:11px;margin-left:6px}
.tchildren{margin-left:14px;border-left:1px solid var(--border);padding-left:4px}
.iconbtn{background:transparent;color:var(--dim);border:1px solid var(--border);padding:6px 9px}
#chips{display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 0}
#chips:empty{display:none}
.chip{display:inline-flex;align-items:center;gap:6px;background:var(--panel2);border:1px solid var(--border);border-radius:12px;padding:2px 8px;font-size:12px;color:var(--text)}
.chip b{font-weight:500}.chip .x{cursor:pointer;color:var(--dim)}
```

> 注：原 `#chat{flex:1;display:flex;flex-direction:column;min-height:0}` 保留不动，新增的 `#chat{flex-direction:row}` 覆盖方向。

- [ ] **步骤 2：改 `#chat` 的 HTML 结构**

把 `web/index.html` 里这段：

```html
    <div id="chat">
      <div id="msgs"><div id="empty">Select or create a session to start.</div></div>
      <div id="composer" class="hidden">
        <textarea id="input" placeholder="Message... (Enter to send, Shift+Enter for newline)"></textarea>
        <button id="send" onclick="sendMsg()">Send</button>
        <button id="stop" class="danger hidden" onclick="stopTurn()">Stop</button>
      </div>
    </div>
```

替换为：

```html
    <div id="chat">
      <div id="chatmain">
        <div id="msgs"><div id="empty">Select or create a session to start.</div></div>
        <div id="composer" class="hidden">
          <div id="chips"></div>
          <div id="composerrow" style="display:flex;gap:8px">
            <input type="file" id="fileinput" class="hidden" onchange="onFileChosen(event)">
            <button id="attach" class="iconbtn" title="" onclick="$('fileinput').click()">📎</button>
            <button id="togglefiles" class="iconbtn" title="Files" onclick="toggleFiles()">📁</button>
            <textarea id="input" placeholder="Message... (Enter to send, Shift+Enter for newline)"></textarea>
            <button id="send" onclick="sendMsg()">Send</button>
            <button id="stop" class="danger hidden" onclick="stopTurn()">Stop</button>
          </div>
        </div>
      </div>
      <div id="filetree" class="hidden">
        <header><b>Files</b><button class="iconbtn" title="Refresh" onclick="loadTree()">⟳</button></header>
        <div class="body" id="treebody"></div>
      </div>
    </div>
```

> `#composer` 现在是纵向（chips 在上、输入行在下）。CSS 里 `#composer{display:flex;...}` 仍生效，方向默认 row——需把它改为 column。在步骤 1 的 CSS 末尾再加一行：`#composer{flex-direction:column;align-items:stretch}`。

- [ ] **步骤 3：加目录树 JS**

在 `<script>` 内 `stopTurn` 函数之后插入：

```javascript
// ---- file tree (REQ2.1) -----------------------------------------------------
function toggleFiles() {
  const ft = $("filetree");
  ft.classList.toggle("hidden");
  if (!ft.classList.contains("hidden")) loadTree();
}
async function loadTree() {
  const body = $("treebody"); body.innerHTML = "";
  await renderInto(body, "");
}
async function renderInto(container, path) {
  let data;
  try { data = await apiJson("/api/files" + (path ? "?path=" + encodeURIComponent(path) : "")); }
  catch (e) { container.textContent = e.message; return; }
  if (!data.entries.length) { container.innerHTML = `<div class="muted">(empty)</div>`; return; }
  for (const e of data.entries) {
    const node = document.createElement("div");
    node.className = "tnode";
    node.textContent = (e.is_dir ? "▸ " : "   ") + e.name;
    if (!e.is_dir) node.innerHTML += `<span class="sz">${e.size}B</span>`;
    container.appendChild(node);
    if (e.is_dir) {
      const kids = document.createElement("div");
      kids.className = "tchildren hidden"; container.appendChild(kids);
      node.onclick = async () => {
        kids.classList.toggle("hidden");
        node.textContent = (kids.classList.contains("hidden") ? "▸ " : "▾ ") + e.name;
        if (!kids.dataset.loaded) { kids.dataset.loaded = "1"; await renderInto(kids, e.path); }
      };
    } else {
      node.onclick = () => previewFile(e.path);
    }
  }
}
async function previewFile(path) {
  let data;
  try { data = await apiJson("/api/files/read?path=" + encodeURIComponent(path)); }
  catch (e) { alert(e.message); return; }
  if (data.content !== undefined) {
    if (confirm(`${data.name} (${data.size}B)\n\n` + data.content.slice(0, 2000) +
                (data.content.length > 2000 ? "\n..." : "") + "\n\n点「确定」下载该文件")) {
      downloadBlob(new Blob([data.content]), data.name);
    }
  } else {
    // 二进制：直接下载
    const a = document.createElement("a");
    a.href = data.data_url; a.download = data.name; a.click();
  }
}
function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href = url; a.download = name; a.click();
  URL.revokeObjectURL(url);
}
```

- [ ] **步骤 4：跑服务手动验证目录树**

运行：`ADMIN_PASSWORD=dev .venv/bin/uvicorn sophagent.main:app --port 8011 &` 然后浏览器开 `http://localhost:8011`，admin/dev 登录。
预期：进入某会话后底部出现 📎/📁 按钮；点 📁 右侧出现 Files 面板；空目录显示 (empty)。停服务：`kill %1`。

（若当前无 agent，先在 Agents 页签建一个再开会话。）

- [ ] **步骤 5：Commit**

```bash
git add web/index.html
git commit -m "feat(M-files): web 右侧只读目录树面板 (REQ2.1)"
```

---

## 任务 4：上传按钮 + 引用进上下文 + tooltip（REQ2.2）

**文件：**
- 修改：`web/index.html`（JS：上传/chip/发送前置/openSession 取 agent）

- [ ] **步骤 1：加上传与 chip JS**

在步骤 3 插入的目录树 JS 之后，继续插入：

```javascript
// ---- upload & attach to context (REQ2.2) ------------------------------------
let attached = [];           // [{name, path}]
let currentAgent = null;     // 当前会话的 agent（用于 read_file 提示）

function fileToDataUrl(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = () => rej(new Error("read failed"));
    r.readAsDataURL(file);
  });
}
async function onFileChosen(ev) {
  const file = ev.target.files[0]; ev.target.value = "";
  if (!file) return;
  try {
    const dataUrl = await fileToDataUrl(file);
    const r = await apiJson("/api/files/upload", {method: "POST",
      body: {path: file.name, data_url: dataUrl}});
    attached.push({name: r.entry.name, path: r.entry.path});
    renderChips();
    if (!$("filetree").classList.contains("hidden")) loadTree();
  } catch (e) { alert("上传失败: " + e.message); }
}
function renderChips() {
  const box = $("chips"); box.innerHTML = "";
  attached.forEach((a, i) => {
    const c = document.createElement("span");
    c.className = "chip";
    c.innerHTML = `📎 <b></b> <span class="x">✕</span>`;
    c.querySelector("b").textContent = a.path;
    c.querySelector(".x").onclick = () => { attached.splice(i, 1); renderChips(); };
    box.appendChild(c);
  });
}
function updateAttachHint() {
  const btn = $("attach");
  const ok = currentAgent && (currentAgent.tools || []).includes("read_file");
  btn.title = ok ? "上传文件并加入对话上下文"
                 : "当前 agent 未启用 read_file，附件可能无法被读取";
  btn.style.opacity = ok ? "1" : ".6";
}
```

- [ ] **步骤 2：发送时前置引用**

在 `sendMsg()` 里，把开头这几行：

```javascript
async function sendMsg() {
  const text = $("input").value.trim();
  if (!text || !currentSid || streaming) return;
  $("input").value = ""; streaming = true;
```

改为（允许"仅附件无文字"也能发，并组装引用前缀）：

```javascript
async function sendMsg() {
  const text = $("input").value.trim();
  if ((!text && !attached.length) || !currentSid || streaming) return;
  const refs = attached.map(a => `[附加文件: ${a.path}]`).join("\n");
  const content = refs ? (refs + (text ? "\n\n" + text : "")) : text;
  attached = []; renderChips();
  $("input").value = ""; streaming = true;
```

然后把该函数体内后续用到的 `text` 替换为 `content`：
- `addBubble("user", text);` → `addBubble("user", content);`
- `body: {content: text}` → `body: {content}`

（函数内仅这两处用 `text` 发送，其余不动。）

- [ ] **步骤 3：openSession 记录当前 agent 并刷新提示**

在 `openSession(sid)` 函数里，`const detail = await apiJson("/api/sessions/" + sid);` 这一行之后插入：

```javascript
  currentAgent = agents.find(a => a.id === detail.agent_id) || null;
  updateAttachHint();
  attached = []; renderChips();
```

- [ ] **步骤 4：跑服务手动验证上传+引用**

运行：`ADMIN_PASSWORD=dev .venv/bin/uvicorn sophagent.main:app --port 8012 &`，浏览器 `http://localhost:8012` 登录，进入一个**启用了 read_file 工具**的 agent 会话。
预期：
1. 点 📎 选一个 .txt 文件 → 输入框上方出现 chip（显示文件名）；右侧 Files 面板（若开着）出现该文件。
2. 输入一句话点 Send → 用户气泡顶部含 `[附加文件: <name>]`；若 agent 真会调用 read_file，可见 🔧 read_file 工具调用。
3. 用一个**没启用 read_file** 的 agent 开会话 → 📎 按钮变淡、悬停提示「当前 agent 未启用 read_file…」。
停服务：`kill %1`。

- [ ] **步骤 5：Commit**

```bash
git add web/index.html
git commit -m "feat(M-files): 上传按钮+附件引用进上下文+read_file 提示 (REQ2.2)"
```

---

## 任务 5：README 与最终验证

**文件：**
- 修改：`README.md`

- [ ] **步骤 1：README 特性补一句**

在 `README.md` 的"特性"列表里，`**内置工具**` 那条之后加一条：

```markdown
- **文件管理**：web 右侧只读目录树（浏览 / 预览 / 下载）；对话框 📎 上传文件进个人工作目录，并以 `[附加文件: <path>]` 引用加入上下文，agent 用 `read_file` 按需读取（参考 hermes）
```

- [ ] **步骤 2：全量测试 + 启动冒烟**

运行：`.venv/bin/python -m pytest -q`
预期：全部 PASS。

运行：`ADMIN_PASSWORD=dev .venv/bin/python -c "from sophagent.main import create_app; create_app()"`
预期：无异常（app 工厂可正常构建）。

- [ ] **步骤 3：Commit**

```bash
git add README.md
git commit -m "docs(M-files): README 补充文件管理特性"
```

---

## 自检结论

- **规格覆盖度：** REQ2.1=任务3（目录树面板）；REQ2.2=任务4（上传+引用+tooltip）；REQ2.3=任务2（仅 `require_user`，无新权限，工作目录天然隔离）；REQ2.4=引用式机制贯穿任务2/4，对标 hermes `/api/files/*`。上传上限/安全=任务1+任务2。
- **占位符扫描：** 无 TODO/待定；每个代码步骤均含完整代码。
- **类型一致性：** 后端 `_entry`/`_safe`/`_root`/`_decode_data_url`/`_dedupe` 定义与调用一致；端点字段 `entry.name`/`entry.path`/`content`/`data_url`/`parent` 在测试与前端用法一致；前端 `attached`(`{name,path}`)、`currentAgent`、`updateAttachHint`/`renderChips`/`loadTree`/`renderInto`/`previewFile` 定义与调用一致。
