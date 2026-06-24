# IM 网关（Telegram）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 新增 sophagent IM 网关：用户在 Telegram 给 bot 发消息 → 网关路由到 sophagent agent（经配对码绑定）→ 流式回复（edit-in-place）推回该 Telegram chat。

**架构：** in-process——Telegram `getUpdates` 轮询作为 lifespan 后台 asyncio task（配 `SOPHAGENT_TELEGRAM_BOT_TOKEN` 才起）。复用共享 turn 核心 `run_turns` + `SessionManager` + `db`。新建 `sophagent/im/` 包：adapter（httpx 直连 Bot API）/ transport（agent 事件→edit-in-place 流式）/ pairing（配对码+绑定）/ commands（/pair /new /stop /help）/ driver（入站调度→run_turns）。`TelegramTransport` 暴露 `on_event(内部事件)`，不走 WS 的 wire 帧。

**技术栈：** FastAPI、httpx（直连 Telegram Bot API，无 SDK）、aiosqlite、pytest。

**参考规格：** `docs/superpowers/specs/2026-06-24-sse-removal-and-im-gateway-design.md` §5。

---

## 文件结构

- 创建：`sophagent/im/__init__.py`（空）
- 创建：`sophagent/im/transport.py` — `TelegramTransport`：内部事件→edit-in-place 流式（限频）
- 创建：`sophagent/im/adapter.py` — `TelegramClient`（httpx：get_updates/send_message/edit_message）+ `run_polling`
- 创建：`sophagent/im/pairing.py` — 配对码签发/消费、绑定 CRUD（db 操作）
- 创建：`sophagent/im/commands.py` — `/pair /new /stop /help` 解析与分发
- 创建：`sophagent/im/driver.py` — `IMDriver.handle_inbound`：命令 or 跑 run_turns(TelegramTransport)
- 创建：`sophagent/api/im_routes.py` — `POST /api/im/pair-code`（签发配对码）
- 修改：`sophagent/db.py` — SCHEMA 加 `im_pair_codes`/`im_bindings` 两表 + 方法
- 修改：`sophagent/config.py` — `telegram_bot_token`、`telegram_allowed_user_ids`
- 修改：`sophagent/api/__init__.py` — 挂载 `im_routes`
- 修改：`sophagent/main.py` — lifespan 启动轮询 task
- 修改：`web/index.html` — 「IM 绑定」入口（选 agent + 生成码）
- 修改：`pyproject.toml` — `httpx` 加主依赖
- 测试：`tests/test_im_pairing.py`、`tests/test_im_transport.py`、`tests/test_im_driver.py`

---

## 任务 1：config + pyproject httpx

**文件：** 修改 `sophagent/config.py`、`pyproject.toml`

- [ ] **步骤 1：config 加两个字段**

`sophagent/config.py` 的 `Config` dataclass，在 `ws_grace_seconds` 之后加：

```python
    # IM 网关（Telegram）：配了 bot token 才启用 in-process 轮询。
    telegram_bot_token: str = ""
    # 额外白名单（逗号分隔的 Telegram user id）；空=仅靠配对码控制访问。
    telegram_allowed_user_ids: tuple[int, ...] = ()
```

`load_config()` 的 `Config(...)` 调用，在 `ws_grace_seconds=...` 之后加：

```python
        telegram_bot_token=os.environ.get("SOPHAGENT_TELEGRAM_BOT_TOKEN", ""),
        telegram_allowed_user_ids=tuple(
            int(u) for u in
            (os.environ.get("SOPHAGENT_TELEGRAM_ALLOWED_USER_IDS") or "").split(",") if u
        ),
```

- [ ] **步骤 2：pyproject 加 httpx 主依赖**

`pyproject.toml` 的 `dependencies = [...]`，追加一行（与 dev 的 httpx 对齐版本）：

```toml
    "httpx>=0.27",
```

- [ ] **步骤 3：验证导入**

运行：`uv sync --extra dev && uv run python -c "from sophagent.config import get_config; c=get_config(); print(c.telegram_bot_token, c.telegram_allowed_user_ids)"`
预期：打印空串和空 tuple，无报错。

- [ ] **步骤 4：Commit**

```bash
git add sophagent/config.py pyproject.toml
git commit -m "feat(im): config 加 telegram_bot_token/allowed_user_ids；httpx 入主依赖"
```

---

## 任务 2：db 两表 + 方法

**文件：** 修改 `sophagent/db.py`；测试 `tests/test_im_pairing.py`（本任务先建空文件，方法测试在任务 4）

- [ ] **步骤 1：SCHEMA 加两表**

`sophagent/db.py` 的 `SCHEMA` 字符串，在 `settings` 表之后、闭合 `"""` 之前加：

```sql
CREATE TABLE IF NOT EXISTS im_pair_codes (
  code       TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  agent_id   INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  used       INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS im_bindings (
  platform   TEXT NOT NULL,
  chat_id    TEXT NOT NULL,
  user_id    INTEGER NOT NULL,
  agent_id   INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (platform, chat_id)
);
```

- [ ] **步骤 2：加 db 方法**

`sophagent/db.py` 的 `Database` 类，在 `effective_` 系列之前（或类末尾）加：

```python
    # -- IM pairing / bindings ---------------------------------------------

    async def create_pair_code(self, user_id: int, agent_id: int, ttl_seconds: int = 600) -> str:
        import secrets as _secrets
        from datetime import datetime, timezone, timedelta
        code = _secrets.token_hex(4)  # 8 字符
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        await self._exec(
            "INSERT INTO im_pair_codes (code, user_id, agent_id, expires_at, used) VALUES (?,?,?,?,0)",
            (code, user_id, agent_id, expires),
        )
        return code

    async def consume_pair_code(self, code: str) -> Optional[tuple[int, int]]:
        """返回 (user_id, agent_id) 或 None（不存在/已用/已过期）。命中即标记 used。"""
        from datetime import datetime, timezone
        row = await self._one(
            "SELECT user_id, agent_id, expires_at, used FROM im_pair_codes WHERE code=?",
            (code,),
        )
        if row is None or row["used"]:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            return None
        await self._exec("UPDATE im_pair_codes SET used=1 WHERE code=?", (code,))
        return row["user_id"], row["agent_id"]

    async def upsert_binding(self, platform: str, chat_id: str, user_id: int,
                             agent_id: int, session_id: str) -> None:
        await self._exec(
            "INSERT INTO im_bindings (platform, chat_id, user_id, agent_id, session_id, created_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(platform, chat_id) DO UPDATE SET"
            " user_id=excluded.user_id, agent_id=excluded.agent_id, session_id=excluded.session_id",
            (platform, chat_id, user_id, agent_id, session_id, now()),
        )

    async def get_binding(self, platform: str, chat_id: str) -> Optional[aiosqlite.Row]:
        return await self._one(
            "SELECT * FROM im_bindings WHERE platform=? AND chat_id=?",
            (platform, chat_id),
        )
```

- [ ] **步骤 3：建空测试文件占位**

`tests/test_im_pairing.py`：

```python
"""IM pairing + binding DB ops tests (filled in task 4)."""
```

- [ ] **步骤 4：跑现有套件确认建表无破坏**

运行：`uv run --extra dev pytest -q`
预期：全绿（新表 IF NOT EXISTS 幂等，新方法未被调用）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/db.py tests/test_im_pairing.py
git commit -m "feat(im): db 加 im_pair_codes/im_bindings 两表 + 方法"
```

---

## 任务 3：TelegramTransport

**文件：** 创建 `sophagent/im/transport.py`、`sophagent/im/__init__.py`；测试 `tests/test_im_transport.py`

`TelegramTransport` 消费 sophagent 内部事件（`text_delta`/`done`/`error`），edit-in-place 流式推到 Telegram。依赖一个 `client`（任务 5 的 `TelegramClient`）提供 `send_message(chat_id, text) -> int` 和 `edit_message(chat_id, message_id, text)`。测试用 fake client。

- [ ] **步骤 1：写失败测试**

`tests/test_im_transport.py`：

```python
"""TelegramTransport: 内部事件 -> edit-in-place 流式（限频）。"""
import asyncio
import pytest
from sophagent.im.transport import TelegramTransport


class FakeClient:
    def __init__(self):
        self.sends = []      # [(chat_id, text)]
        self.edits = []      # [(chat_id, msg_id, text)]

    async def send_message(self, chat_id, text):
        msg_id = len(self.sends) + 1
        self.sends.append((chat_id, text))
        return msg_id

    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))


@pytest.mark.asyncio
async def test_first_delta_sends_then_edits_on_done():
    client = FakeClient()
    t = TelegramTransport(chat_id="42", client=client, min_edit_interval=0)
    await t.on_event({"type": "text_delta", "text": "hel"})
    await t.on_event({"type": "text_delta", "text": "lo"})
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})
    assert client.sends == [("42", "hel")]
    assert len(client.edits) == 1
    assert client.edits[0][2] == "hello"


@pytest.mark.asyncio
async def test_reasoning_and_tool_ignored():
    client = FakeClient()
    t = TelegramTransport("42", client, min_edit_interval=0)
    await t.on_event({"type": "reasoning_delta", "text": "thinking"})
    await t.on_event({"type": "tool_call", "id": "c1", "name": "x", "arguments": {}})
    await t.on_event({"type": "tool_result", "id": "c1", "name": "x", "preview": "r"})
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})
    assert client.sends == []  # 没有任何正文
    assert client.edits == []


@pytest.mark.asyncio
async def test_chained_turn_starts_new_message():
    client = FakeClient()
    t = TelegramTransport("42", client, min_edit_interval=0)
    # turn 1
    await t.on_event({"type": "text_delta", "text": "a"})
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})
    # queued_next + turn 2
    await t.on_event({"type": "queued_next", "content": "q"})
    await t.on_event({"type": "text_delta", "text": "b"})
    await t.on_event({"type": "done", "usage": {}, "context_length": 0, "context_limit": 0})
    assert [s[1] for s in client.sends] == ["a", "b"]  # 两条独立消息
```

- [ ] **步骤 2：运行验证失败**

运行：`uv run --extra dev pytest tests/test_im_transport.py -q`
预期：FAIL（模块不存在）。

- [ ] **步骤 3：实现 transport**

`sophagent/im/__init__.py`：空文件。

`sophagent/im/transport.py`：

```python
"""TelegramTransport: 内部 agent 事件 -> Telegram edit-in-place 流式。

消费 sophagent 内部事件（text_delta/done/error/...），不使用 WS 的 wire 帧。
首轮首帧 sendMessage 记 message_id；后续累积文本 editMessageText（限频）；
done 落最终全文并清 message_id（下一轮首帧重发新消息）。
reasoning/tool/turn_usage/session.info/queued_next 忽略。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol


class TelegramLikeClient(Protocol):
    async def send_message(self, chat_id: str, text: str) -> int: ...
    async def edit_message(self, chat_id: str, message_id: int, text: str) -> None: ...


class TelegramTransport:
    def __init__(self, chat_id: str, client: TelegramLikeClient,
                 *, min_edit_interval: float = 0.6) -> None:
        self.chat_id = chat_id
        self.client = client
        self.min_edit_interval = min_edit_interval
        self.message_id: int | None = None
        self._text = ""
        self._last_edit_at = 0.0

    async def on_event(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "text_delta":
            self._text += ev.get("text", "")
            await self._flush(force=False)
        elif t == "done":
            await self._flush(force=True)            # 落最终全文
            self.message_id = None
            self._text = ""
        elif t == "error":
            msg = ev.get("message", "error")
            if self.message_id is None:
                await self.client.send_message(self.chat_id, msg)
                # 不记 message_id：错误是终结，下一轮若来 text_delta 自然新发
                self.message_id = None
            else:
                await self.client.edit_message(self.chat_id, self.message_id, msg)
                self.message_id = None
            self._text = ""
        # reasoning_delta / tool_call / tool_result / turn_usage / session_info / queued_next: 忽略

    async def _flush(self, *, force: bool) -> None:
        if not self._text:
            return
        now = time.monotonic()
        if self.message_id is None:
            self.message_id = await self.client.send_message(self.chat_id, self._text)
            self._last_edit_at = now
            return
        if not force and now - self._last_edit_at < self.min_edit_interval:
            return  # 限频：等下次或 done
        await self.client.edit_message(self.chat_id, self.message_id, self._text)
        self._last_edit_at = now

    async def close(self) -> None:
        await self._flush(force=True)  # 兜底落全文
```

- [ ] **步骤 4：运行验证通过**

运行：`uv run --extra dev pytest tests/test_im_transport.py -q`
预期：PASS（3 个用例）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/im/__init__.py sophagent/im/transport.py tests/test_im_transport.py
git commit -m "feat(im): TelegramTransport（内部事件->edit-in-place 流式，限频）"
```

---

## 任务 4：pairing

**文件：** 创建 `sophagent/im/pairing.py`；测试 `tests/test_im_pairing.py`

- [ ] **步骤 1：写测试**

`tests/test_im_pairing.py`（替换占位）：

```python
"""IM pairing + binding DB ops."""
import pytest
from datetime import datetime, timezone, timedelta
from sophagent.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "t.db")
    await d.connect()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_create_and_consume_pair_code(db):
    code = await db.create_pair_code(user_id=1, agent_id=2)
    assert len(code) == 8
    got = await db.consume_pair_code(code)
    assert got == (1, 2)
    # 一次性：再用无效
    assert await db.consume_pair_code(code) is None


@pytest.mark.asyncio
async def test_consume_unknown_returns_none(db):
    assert await db.consume_pair_code("deadbeef") is None


@pytest.mark.asyncio
async def test_expired_code_returns_none(db):
    code = await db.create_pair_code(user_id=1, agent_id=2, ttl_seconds=-1)
    assert await db.consume_pair_code(code) is None


@pytest.mark.asyncio
async def test_upsert_and_get_binding(db):
    await db.upsert_binding("telegram", "chat-1", user_id=1, agent_id=2, session_id="s1")
    row = await db.get_binding("telegram", "chat-1")
    assert row["user_id"] == 1 and row["agent_id"] == 2 and row["session_id"] == "s1"
    # 覆盖（换 agent/session）
    await db.upsert_binding("telegram", "chat-1", user_id=1, agent_id=3, session_id="s2")
    row = await db.get_binding("telegram", "chat-1")
    assert row["agent_id"] == 3 and row["session_id"] == "s2"


@pytest.mark.asyncio
async def test_get_binding_missing(db):
    assert await db.get_binding("telegram", "nope") is None
```

`conftest.py` 的 pytest-asyncio 配置：检查 `pyproject.toml` 是否有 `asyncio_mode`。若无，在 `tests/test_im_pairing.py` 顶部加 `pytestmark = pytest.mark.asyncio` 或用 `@pytest.mark.asyncio`（已加）。确认 `pyproject.toml` 有 `[tool.pytest.ini_options] asyncio_mode = "auto"`——若无，本任务步骤 2 补。

- [ ] **步骤 2：确认 pytest-asyncio 模式**

运行：`grep -A3 "pytest.ini_options\|asyncio_mode" pyproject.toml`
若没有 `asyncio_mode = "auto"`，在 `[tool.pytest.ini_options]` 下加 `asyncio_mode = "auto"`（若无该 section 则新建）。这样新的 async 测试不用逐个加 marker。加完后，任务 3 的 `@pytest.mark.asyncio` 可保留（无害）。

- [ ] **步骤 3：实现 pairing**

`sophagent/im/pairing.py`：

```python
"""配对码签发/消费、IM 绑定 CRUD。

db 方法在 sophagent/db.py；本模块是薄封装，集中 IM 语义（platform 常量、
session 创建）供 driver/commands 调用。
"""

from __future__ import annotations

import uuid
from typing import Optional

PLATFORM = "telegram"


async def issue_code(db, user_id: int, agent_id: int) -> str:
    return await db.create_pair_code(user_id, agent_id)


async def consume_code(db, code: str) -> Optional[tuple[int, int]]:
    return await db.consume_pair_code(code)


async def bind(db, chat_id: str, user_id: int, agent_id: int) -> str:
    """为 (chat_id, user, agent) 建新 sophagent session 并写绑定。返回 session_id。
    若已有绑定则覆盖（换 agent 时新建 session）。"""
    agent = await db.get_agent(agent_id)
    group_id = agent["group_id"] if agent else None
    session_id = uuid.uuid4().hex
    await db.create_session(session_id, user_id, agent_id, group_id, title="")
    await db.upsert_binding(PLATFORM, chat_id, user_id, agent_id, session_id)
    return session_id


async def renew_session(db, chat_id: str) -> Optional[str]:
    """为已绑定的 chat 新建一个 session（/new）。返回新 session_id 或 None（未绑定）。"""
    row = await db.get_binding(PLATFORM, chat_id)
    if row is None:
        return None
    return await bind(db, chat_id, row["user_id"], row["agent_id"])


async def lookup(db, chat_id: str):
    """返回绑定 row 或 None。"""
    return await db.get_binding(PLATFORM, chat_id)
```

- [ ] **步骤 4：运行验证通过**

运行：`uv run --extra dev pytest tests/test_im_pairing.py -q`
预期：PASS（5 个用例）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/im/pairing.py tests/test_im_pairing.py pyproject.toml
git commit -m "feat(im): pairing（配对码签发/消费、绑定 CRUD、session 创建）"
```

---

## 任务 5：adapter（httpx Telegram Bot API）

**文件：** 创建 `sophagent/im/adapter.py`；测试 `tests/test_im_driver.py` 占位（任务 7 填）

`TelegramClient` 封装三个 Bot API 调用。`run_polling` 长轮询 `getUpdates`，把每条消息交给 driver。

- [ ] **步骤 1：实现 adapter**

`sophagent/im/adapter.py`：

```python
"""Telegram Bot API client (httpx, no SDK) + long-poll loop.

只实现 IM 网关需要的子集：getUpdates / sendMessage / editMessageText。
adapter 与 transport 分离：adapter 管 HTTP，transport 管事件->投递语义。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.telegram.org/bot{token}/{method}"


@dataclass
class MessageEvent:
    text: str
    chat_id: str
    from_id: str


class TelegramClient:
    """httpx 直连 Bot API。供 TelegramTransport 与 run_polling 共用。"""

    def __init__(self, token: str, *, timeout: float = 35.0) -> None:
        self.token = token
        self.timeout = timeout

    async def send_message(self, chat_id: str, text: str) -> int:
        r = await self._call("sendMessage", {"chat_id": chat_id, "text": text})
        return r["result"]["message_id"]

    async def edit_message(self, chat_id: str, message_id: int, text: str) -> None:
        try:
            await self._call("editMessageText",
                             {"chat_id": chat_id, "message_id": message_id, "text": text})
        except TelegramError as e:
            # "message is not modified" 等可忽略
            if "not modified" not in str(e).lower():
                raise

    async def get_updates(self, offset: int) -> list[dict[str, Any]]:
        r = await self._call("getUpdates", {"offset": offset, "timeout": 30},
                             timeout=self.timeout)
        return r["result"]

    async def _call(self, method: str, params: dict[str, Any], *, timeout: float | None = None) -> dict:
        url = BASE.format(token=self.token, method=method)
        async with httpx.AsyncClient(timeout=timeout or 30) as c:
            resp = await c.post(url, json=params)
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(data.get("description", "telegram error"))
        return data


class TelegramError(Exception):
    pass


async def run_polling(client: TelegramClient, driver, *, allowed_user_ids: tuple[int, ...] = ()) -> None:
    """长轮询 getUpdates，把文本消息交给 driver.handle_inbound。"""
    offset = 0
    log.info("telegram polling started")
    try:
        while True:
            try:
                updates = await client.get_updates(offset)
            except Exception as e:
                log.warning("getUpdates failed: %s; retry in 2s", e)
                await asyncio.sleep(2)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if not msg:
                    continue
                text = (msg.get("text") or "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                from_id = str(msg.get("from", {}).get("id", ""))
                if not text or not chat_id:
                    continue
                if allowed_user_ids and from_id not in {str(i) for i in allowed_user_ids}:
                    continue
                try:
                    await driver.handle_inbound(MessageEvent(text=text, chat_id=chat_id, from_id=from_id),
                                                client=client)
                except Exception:
                    log.exception("handle_inbound failed chat=%s", chat_id)
    finally:
        log.info("telegram polling stopped")
```

- [ ] **步骤 2：建空测试占位**

`tests/test_im_driver.py`：

```python
"""IMDriver inbound -> run_turns -> TelegramTransport (mock client). Filled in task 7."""
```

- [ ] **步骤 3：导入冒烟**

运行：`uv run python -c "from sophagent.im.adapter import TelegramClient, run_polling, MessageEvent; print('ok')"`
预期：打印 ok。

- [ ] **步骤 4：Commit**

```bash
git add sophagent/im/adapter.py tests/test_im_driver.py
git commit -m "feat(im): adapter（httpx Telegram Bot API + getUpdates 长轮询）"
```

---

## 任务 6：commands

**文件：** 创建 `sophagent/im/commands.py`；测试并入 `tests/test_im_driver.py`（任务 7）

- [ ] **步骤 1：实现 commands**

`sophagent/im/commands.py`：

```python
"""IM 斜杠命令：/pair /new /stop /help。

仅这 4 条在 IM 暴露；sophagent 现有 /compact /model 等不在 IM 暴露（避免冲突）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CommandResult:
    handled: bool
    text: str = ""           # 回复给 IM 用户的内容
    name: str = ""           # 命令名（供 driver 记日志）


def parse(text: str) -> tuple[str, str]:
    """'/pair abc' -> ('pair', 'abc')；非命令返回 ('', '')。"""
    if not text.startswith("/"):
        return "", ""
    parts = text.split(maxsplit=1)
    name = parts[0].lstrip("/").lower()
    args = parts[1].strip() if len(parts) > 1 else ""
    return name, args
```

命令的**执行**涉及 db/manager/driver 状态，放在 `driver.py`（任务 7）的 `handle_inbound` 里按 name 分发，不在本模块做副作用。本模块只做解析。

- [ ] **步骤 2：导入冒烟**

运行：`uv run python -c "from sophagent.im.commands import parse; print(parse('/pair abc123'), parse('hi'), parse('/new'))"`
预期：`('pair', 'abc123') ('', '') ('new', '')`

- [ ] **步骤 3：Commit**

```bash
git add sophagent/im/commands.py
git commit -m "feat(im): commands 解析（/pair /new /stop /help）"
```

---

## 任务 7：driver

**文件：** 创建 `sophagent/im/driver.py`；测试 `tests/test_im_driver.py`

`IMDriver.handle_inbound`：解析命令（/pair→绑定、/new→新会话、/stop→中断、/help→提示），否则查绑定→跑 `run_turns(TelegramTransport)`。单 chat 串行复用 `manager.lock_for(session_id)`。

- [ ] **步骤 1：写测试**

`tests/test_im_driver.py`（替换占位）。用 conftest 的 `client` fixture（EchoProvider）拿 db/manager/skill_store，driver 直接用 `client.app.state`。async 测试（任务 4 已开 `asyncio_mode=auto`，无需逐个加 marker，但加了无害）。

```python
"""IMDriver: 入站消息 -> 命令 or run_turns(TelegramTransport) with mock client."""
import pytest
from sophagent.im.driver import IMDriver
from sophagent.im import pairing
from sophagent.im.adapter import MessageEvent


class FakeTelegramClient:
    def __init__(self):
        self.sends, self.edits = [], []
    async def send_message(self, chat_id, text):
        mid = len(self.sends) + 1
        self.sends.append((chat_id, text)); return mid
    async def edit_message(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))


def _state(client):
    return client.app.state


def _me_id(client, bob):
    return client.get("/api/auth/me", headers=bob).json()["id"]


async def _bind(client, bob, agent_id, chat_id):
    s = _state(client)
    uid = _me_id(client, bob)
    code = await pairing.issue_code(s.db, user_id=uid, agent_id=agent_id)
    await pairing.consume_code(s.db, code)  # 模拟 IM 端 /pair 已消费
    return await pairing.bind(s.db, chat_id, user_id=uid, agent_id=agent_id)


def _ev(text, chat_id):
    return MessageEvent(text=text, chat_id=chat_id, from_id="1")


@pytest.mark.asyncio
async def test_help_command(client, bob, agent_id):
    await _bind(client, bob, agent_id, "chat-1")
    driver = IMDriver(db=_state(client).db, manager=_state(client).manager,
                      skill_store=_state(client).skill_store)
    tc = FakeTelegramClient()
    await driver.handle_inbound(_ev("/help", "chat-1"), client=tc)
    assert any("pair" in s[1] for s in tc.sends)


@pytest.mark.asyncio
async def test_unbound_chat_replies_with_pair_hint(client, bob, agent_id):
    driver = IMDriver(db=_state(client).db, manager=_state(client).manager,
                      skill_store=_state(client).skill_store)
    tc = FakeTelegramClient()
    await driver.handle_inbound(_ev("hi", "unbound-chat"), client=tc)
    assert any("/pair" in s[1] for s in tc.sends)


@pytest.mark.asyncio
async def test_bound_chat_runs_turn_and_edits(client, bob, agent_id):
    await _bind(client, bob, agent_id, "chat-2")
    driver = IMDriver(db=_state(client).db, manager=_state(client).manager,
                      skill_store=_state(client).skill_store)
    tc = FakeTelegramClient()
    await driver.handle_inbound(_ev("hello", "chat-2"), client=tc)
    # EchoProvider 回 "echo: hello"：首帧 sendMessage + done 落全文 edit
    assert tc.sends[0][1] == "echo: hello" or any("echo: hello" in e[2] for e in tc.edits)
```

- [ ] **步骤 2：运行验证失败**

运行：`uv run --extra dev pytest tests/test_im_driver.py -q`
预期：FAIL（`IMDriver` 不存在）。

- [ ] **步骤 3：实现 driver**

`sophagent/im/driver.py`：

```python
"""IM inbound driver: 命令分发 + run_turns(TelegramTransport)。

入站 MessageEvent -> 解析 /pair /new /stop /help，否则查绑定 -> 跑共享
turn 核心 run_turns，事件经 TelegramTransport edit-in-place 推回 chat。
单 chat 串行（manager.lock_for(session_id)）。排队复用 manager.try_put。
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any

from ..agent.turn import run_turns
from . import pairing
from .adapter import MessageEvent
from .commands import parse
from .transport import TelegramTransport

log = logging.getLogger(__name__)

HELP_TEXT = "命令：/pair <code> 绑定 · /new 新会话 · /stop 停止 · /help"


class IMDriver:
    def __init__(self, db, manager, skill_store) -> None:
        self.db = db
        self.manager = manager
        self.skill_store = skill_store

    async def handle_inbound(self, ev: MessageEvent, *, client) -> None:
        name, args = parse(ev.text)
        if name:
            await self._dispatch_command(name, args, ev, client)
            return
        await self._handle_text(ev, client)

    async def _dispatch_command(self, name: str, args: str, ev: MessageEvent, client) -> None:
        if name == "help":
            await client.send_message(ev.chat_id, HELP_TEXT)
        elif name == "pair":
            await self._cmd_pair(args, ev, client)
        elif name == "new":
            await self._cmd_new(ev, client)
        elif name == "stop":
            await self._cmd_stop(ev, client)
        else:
            await client.send_message(ev.chat_id, f"未知命令 /{name}。{HELP_TEXT}")

    async def _cmd_pair(self, args: str, ev: MessageEvent, client) -> None:
        if not args:
            await client.send_message(ev.chat_id, "用法：/pair <配对码>")
            return
        pair = await pairing.consume_code(self.db, args.strip())
        if pair is None:
            await client.send_message(ev.chat_id, "配对码无效或已过期。")
            return
        user_id, agent_id = pair
        agent = await self.db.get_agent(agent_id)
        await pairing.bind(self.db, ev.chat_id, user_id, agent_id)
        await client.send_message(ev.chat_id,
            f"✅ 已绑定，agent={agent['name'] if agent else agent_id}。发消息开始对话。")

    async def _cmd_new(self, ev: MessageEvent, client) -> None:
        sid = await pairing.renew_session(self.db, ev.chat_id)
        if sid is None:
            await client.send_message(ev.chat_id, "未绑定，请先 /pair <配对码>。")
            return
        await client.send_message(ev.chat_id, "✅ 已开启新会话。")

    async def _cmd_stop(self, ev: MessageEvent, client) -> None:
        row = await pairing.lookup(self.db, ev.chat_id)
        if row is None:
            await client.send_message(ev.chat_id, "未绑定。")
            return
        stopped = self.manager.stop(row["session_id"])
        self.manager.clear_queue(row["session_id"])
        await client.send_message(ev.chat_id, "⏹ 已停止。" if stopped else "（无在跑的回复）")

    async def _handle_text(self, ev: MessageEvent, client) -> None:
        row = await pairing.lookup(self.db, ev.chat_id)
        if row is None:
            await client.send_message(ev.chat_id, "未绑定，请先 /pair <配对码>。")
            return
        session_id = row["session_id"]
        user_id = row["user_id"]
        if self.manager.is_busy(session_id):
            if not self.manager.try_put(session_id, ev.text):
                await client.send_message(ev.chat_id, "队列已满，请稍后再试。")
            return
        transport = TelegramTransport(ev.chat_id, client)
        task = asyncio.create_task(self._run(session_id, ev.text, user_id, transport))
        self.manager.register_task(session_id, task)

    async def _run(self, session_id, user_input, user_id, transport):
        # 注意：不要再 `async with self.manager.lock_for(session_id)`——
        # run_turns 内部已加锁（见 agent/turn.py），asyncio.Lock 不可重入会自死锁。
        try:
            async for ev in run_turns(session_id=session_id, user_input=user_input,
                                      db=self.db, manager=self.manager,
                                      skill_store=self.skill_store, user_id=user_id):
                await transport.on_event(ev)
        except asyncio.CancelledError:
            await transport.on_event({"type": "error", "message": "stopped by user"})
            raise
        except Exception as e:
            log.exception("im turn failed session=%s", session_id)
            await transport.on_event({"type": "error", "message": f"出错：{e}"})
        finally:
            await transport.close()
```

- [ ] **步骤 4：运行验证通过**

运行：`uv run --extra dev pytest tests/test_im_driver.py -q`
预期：PASS（3 个用例）。

- [ ] **步骤 5：Commit**

```bash
git add sophagent/im/driver.py tests/test_im_driver.py
git commit -m "feat(im): driver（入站调度：命令分发 + run_turns(TelegramTransport)）"
```

---

## 任务 8：REST 配对码路由 + 前端入口

**文件：** 创建 `sophagent/api/im_routes.py`；修改 `sophagent/api/__init__.py`、`web/index.html`

- [ ] **步骤 1：REST 路由**

`sophagent/api/im_routes.py`：

```python
"""IM 绑定 REST：sophagent 用户签发配对码（供 Telegram 端 /pair 用）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import require_user
from ..im import pairing
from ..perms import can_access_group

router = APIRouter()


class PairCodeRequest(BaseModel):
    agent_id: int


@router.post("/pair-code")
async def issue_pair_code(req: PairCodeRequest, request: Request, user=Depends(require_user)):
    db = request.app.state.db
    agent = await db.get_agent(req.agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    # 仅 agent 所属 group 的成员/owner 可签发（与 session chat 访问模型一致）
    if not await can_access_group(db, agent["group_id"], user["id"]):
        raise HTTPException(403, "cannot use this agent")
    code = await pairing.issue_code(db, user["id"], req.agent_id)
    return {"code": code, "expires_in": 600}
```

- [ ] **步骤 2：挂载路由**

`sophagent/api/__init__.py`：在 import 块加 `im_routes,`；`mount_routes` 加：

```python
    app.include_router(im_routes.router, prefix="/api/im", tags=["im"])
```

- [ ] **步骤 3：前端「IM 绑定」入口**

`web/index.html`：在 `#tabs` 里加一个 tab 按钮，加一个隐藏的 `#im` 面板。最小实现——选 agent + 生成码 + 展示。

先读 `web/index.html` 找 `#tabs` 与 `#admin` 面板位置（grep `id="tabs"` 与 `id="admin"`）。在 `#tabs` 末尾加：

```html
<button onclick="showTab('im')" id="tab-im" class="hidden">IM 绑定</button>
```

在 `<div id="admin" class="hidden"></div>` 之后加：

```html
<div id="im" class="hidden" style="padding:16px;overflow-y:auto">
  <h3>Telegram 绑定</h3>
  <p style="color:var(--dim);font-size:13px">选一个 agent 生成配对码，在 Telegram bot 发 <code>/pair &lt;码&gt;</code> 绑定。</p>
  <label>Agent</label>
  <select id="im-agent"></select>
  <button onclick="genPairCode()">生成配对码</button>
  <div id="im-result" style="margin-top:12px"></div>
</div>
```

在 `boot()` 里 `$("tab-admin").classList.toggle(...)` 之后加：

```javascript
  $("tab-im").classList.toggle("hidden", !me);
```

（任何登录用户都可见 IM 绑定 tab。）

加函数（放在 `logout` 附近）：

```javascript
async function showImPanel() {
  const sel = $("im-agent");
  const agents = await apiJson("/api/agents");
  sel.innerHTML = agents.map(a => `<option value="${a.id}">${a.name}</option>`).join("");
}
async function genPairCode() {
  const aid = $("im-agent").value;
  const r = await apiJson("/api/im/pair-code", {method: "POST", body: {agent_id: +aid}});
  $("im-result").innerHTML = `配对码：<b style="font-size:18px">${r.code}</b><br>在 Telegram bot 发：<code>/pair ${r.code}</code>（${r.expires_in}s 内有效）`;
}
```

把 `showTab` 调整为切到 im 时调 `showImPanel()`——读现有 `showTab` 实现，在 `tab === 'im'` 分支调 `showImPanel()`（若无 switch 则在 showTab 末尾 `if (t==='im') showImPanel();`）。先 grep `function showTab` 读其实现再改。

- [ ] **步骤 4：测试路由**

加 `tests/test_im_routes.py`：

```python
"""IM pair-code REST route."""


def test_issue_pair_code(client, bob, agent_id):
    r = client.post("/api/im/pair-code", json={"agent_id": agent_id}, headers=bob)
    assert r.status_code == 200
    code = r.json()["code"]
    assert len(code) == 8


def test_issue_pair_code_unknown_agent(client, bob):
    r = client.post("/api/im/pair-code", json={"agent_id": 99999}, headers=bob)
    assert r.status_code == 404
```

运行：`uv run --extra dev pytest tests/test_im_routes.py -q`
预期：PASS。

- [ ] **步骤 5：前端 JS 语法校验**

运行：
```bash
python3 -c "import re; s=re.findall(r'<script>(.*?)</script>', open('web/index.html').read(), re.S)[-1]; open('/tmp/ui_im.js','w').write(s)"
node --check /tmp/ui_im.js && echo "JS OK"
```
预期：JS OK。

- [ ] **步骤 6：Commit**

```bash
git add sophagent/api/im_routes.py sophagent/api/__init__.py web/index.html tests/test_im_routes.py
git commit -m "feat(im): REST /api/im/pair-code + 前端「IM 绑定」入口"
```

---

## 任务 9：main.py lifespan 启动轮询

**文件：** 修改 `sophagent/main.py`

- [ ] **步骤 1：lifespan 启动轮询 task**

`sophagent/main.py` 的 `lifespan`，在 `yield` 之前（registry refresh 之后）加：

```python
    # IM 网关（Telegram）：配了 token 才起 in-process 轮询
    im_task = None
    if cfg.telegram_bot_token:
        from .im.adapter import TelegramClient, run_polling
        from .im.driver import IMDriver
        tg_client = TelegramClient(cfg.telegram_bot_token)
        im_driver = IMDriver(db=db, manager=app.state.manager, skill_store=app.state.skill_store)
        im_task = asyncio.create_task(
            run_polling(tg_client, im_driver, allowed_user_ids=cfg.telegram_allowed_user_ids)
        )
        log.info("IM gateway (telegram) polling started")
```

`lifespan` 顶部已有 `import asyncio`？检查；若无则加。`lifespan` 的 `yield` 之后（`await db.close()` 之前）加清理：

```python
    if im_task is not None:
        im_task.cancel()
        try:
            await im_task
        except asyncio.CancelledError:
            pass
```

- [ ] **步骤 2：导入冒烟**

运行：`uv run python -c "from sophagent.main import create_app; create_app(); print('ok')"`
预期：打印 ok（未配 token，不启轮询）。

- [ ] **步骤 3：Commit**

```bash
git add sophagent/main.py
git commit -m "feat(im): lifespan 启动 Telegram 轮询（配 token 才起）"
```

---

## 任务 10：集成 + 全量 + docker 冒烟

**文件：** 无（验证）

- [ ] **步骤 1：全量测试**

运行：`uv run --extra dev pytest -q`
预期：全绿（含 im pairing/transport/driver/routes 测试）。

- [ ] **步骤 2：docker 重建冒烟（不配 token，IM 不起，确认无破坏）**

```bash
SOPHAGENT_PORT=8849 docker compose -p sophagent-sse-im up --build -d
for i in $(seq 1 25); do curl -sf http://127.0.0.1:8849/healthz >/dev/null 2>&1 && break; sleep 1; done
```

确认：healthz ok；`POST /api/im/pair-code` 返回配对码；WS 对话仍正常（REQ1 未回归）。

- [ ] **步骤 3：真机 Telegram 冒烟（可选，需用户提供 bot token）**

若用户提供 `SOPHAGENT_TELEGRAM_BOT_TOKEN`：
```bash
SOPHAGENT_PORT=8849 SOPHAGENT_TELEGRAM_BOT_TOKEN=<token> docker compose -p sophagent-sse-im up --build -d
```
在 web UI「IM 绑定」生成码 → Telegram bot 发 `/pair <码>` → 发消息 → 看流式 edit 回复。

- [ ] **步骤 4：Commit（无代码改动则跳过）**

无。

---

## 自检

**规格覆盖度（spec §5）：**
- §5.1 模块划分 im/{transport,adapter,pairing,driver,commands} → 任务 3-7 ✓
- §5.2 数据流 → driver 任务 7 ✓
- §5.3 入站 adapter getUpdates → 任务 5 ✓
- §5.4 出站 transport edit-in-place 限频 → 任务 3 ✓
- §5.5 driver 调度（命令/绑定/排队/lock）→ 任务 7 ✓
- §5.6 配对 + DB 两表 → 任务 2、4 ✓
- §5.7 命令 /pair /new /stop /help → 任务 6、7 ✓
- §5.8 lifespan 启动 → 任务 9 ✓
- §5.9 config → 任务 1 ✓
- §5.10 httpx → 任务 1 ✓
- §6 测试（pairing/transport/driver/集成）→ 任务 3、4、7、10 ✓
- web UI「IM 绑定」→ 任务 8 ✓

**占位符扫描：** 已清理所有"两版本/冗余"块：任务 2 `create_pair_code`（单行 expires）、任务 7 driver `_run`（不套 lock，附原因注释）、任务 7 测试（async 版）、任务 8 路由（带 Request 的最终版）。无 TODO/待定/NotImplementedError。

**类型一致性：** `TelegramTransport(chat_id, client, *, min_edit_interval)` 在任务 3 定义，任务 7 driver 用 `TelegramTransport(ev.chat_id, client)` 一致。`client` 协议 `send_message(chat_id, text)->int` / `edit_message(chat_id, message_id, text)` 在任务 3（Protocol）、任务 5（TelegramClient）、测试 FakeClient 三处一致。`MessageEvent(text, chat_id, from_id)` 任务 5 定义，任务 7 用。`pairing.issue_code/consume_code/bind/renew_session/lookup` 任务 4 定义，任务 7/8 用。`IMDriver(db, manager, skill_store)` 任务 7 定义，任务 9 lifespan 用。`run_turns` 签名与 `agent/turn.py` 一致。

**遗漏：** spec §5.5 提到 driver 单 chat 串行复用 `manager.lock_for(session_id)`——run_turns 内部已加锁，driver 不再套（任务 7 已注明，防自死锁）。spec §5.7 `/new` 更新 `im_bindings.session_id`——`pairing.bind` 的 upsert 覆盖 session_id（任务 4 ✓）。
