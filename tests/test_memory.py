"""记忆增强（子项目①）测试：双存储 target、总字符容量与满额整合、去重、
旧库迁移、双段 system prompt 注入。全部使用 fake provider / 临时库。"""

from __future__ import annotations

import pytest

from sophclaw import config as config_mod
from sophclaw.db import Database


# -- 单元 1：纯函数 check_write（容量/去重/单条上限分类） --------------------


def test_check_write_ok():
    from sophclaw.tools.memory import check_write

    r = check_write(
        new_content="hello", existing_contents=["a"], used_chars=1,
        max_chars=500, total_limit=2200,
    )
    assert r.status == "ok"


def test_check_write_too_long():
    from sophclaw.tools.memory import check_write

    r = check_write(
        new_content="x" * 501, existing_contents=[], used_chars=0,
        max_chars=500, total_limit=2200,
    )
    assert r.status == "too_long"


def test_check_write_duplicate():
    from sophclaw.tools.memory import check_write

    r = check_write(
        new_content="same", existing_contents=["same"], used_chars=4,
        max_chars=500, total_limit=2200,
    )
    assert r.status == "duplicate"


def test_check_write_over_capacity_reports_usage():
    from sophclaw.tools.memory import check_write

    r = check_write(
        new_content="y" * 100, existing_contents=["z" * 2150], used_chars=2150,
        max_chars=500, total_limit=2200,
    )
    assert r.status == "over_capacity"
    assert r.used == 2150
    assert r.limit == 2200


# -- 单元 2：DB 层双存储 target ---------------------------------------------


async def _make_db(tmp_path) -> Database:
    db = Database(tmp_path / "m.db")
    await db.connect()
    # memory 行有外键到 users，建一个用户满足约束
    await db._exec(
        "INSERT INTO users (id, username, password_hash, created_at) VALUES (1,'u','h','t')"
    )
    await db.conn.commit()
    return db


@pytest.mark.asyncio
async def test_memory_add_list_per_target(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    db = await _make_db(tmp_path)
    try:
        await db.memory_add(1, "agent note", target="memory")
        await db.memory_add(1, "user likes terse", target="user")
        mem = [r["content"] for r in await db.memory_list(1, target="memory")]
        usr = [r["content"] for r in await db.memory_list(1, target="user")]
        assert mem == ["agent note"]
        assert usr == ["user likes terse"]
        # target=None 返回全部
        allrows = await db.memory_list(1)
        assert len(allrows) == 2
        assert {r["target"] for r in allrows} == {"memory", "user"}
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_memory_default_target_is_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    db = await _make_db(tmp_path)
    try:
        await db.memory_add(1, "no target given")
        rows = await db.memory_list(1, target="memory")
        assert [r["content"] for r in rows] == ["no target given"]
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_migration_adds_target_column_old_rows_become_memory(tmp_path, monkeypatch):
    """旧库（memory 表无 target 列）启动后应加列，旧条目归入 'memory'。"""
    import aiosqlite

    path = tmp_path / "old.db"
    # 手工建一个不含 target 列的旧 memory 表 + users
    conn = await aiosqlite.connect(path)
    await conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, created_at TEXT);
        INSERT INTO users (id, username, password_hash, created_at) VALUES (1,'u','h','t');
        CREATE TABLE memory (
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, content TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO memory (user_id, content, created_at, updated_at)
          VALUES (1, 'legacy entry', 't', 't');
        """
    )
    await conn.commit()
    await conn.close()

    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    db = Database(path)
    await db.connect()  # 触发 _migrate_structure
    try:
        cols = await db._columns("memory")
        assert "target" in cols
        rows = await db.memory_list(1, target="memory")
        assert [r["content"] for r in rows] == ["legacy entry"]
    finally:
        await db.close()
        config_mod.reset_config()


# -- 单元 4：memory 工具（双 target、整合提示、去重、安全） ------------------


async def _tool_ctx(tmp_path, monkeypatch):
    """构造一个带真实 DB 的 ToolContext，直接调用 memory 处理器。"""
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    db = await _make_db(tmp_path)
    from sophclaw.models import AgentDef
    from sophclaw.tools.registry import ToolContext

    agent = AgentDef(
        id=1, name="t", description="", system_prompt="p",
        provider="test", model="m", tools=["memory"], skills=None,
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(user_id=1, workspace=ws, agent=agent, db=db), db


@pytest.mark.asyncio
async def test_tool_writes_to_named_target(tmp_path, monkeypatch):
    from sophclaw.tools.memory import memory

    ctx, db = await _tool_ctx(tmp_path, monkeypatch)
    try:
        await memory(ctx, action="add", content="prefers terse", target="user")
        await memory(ctx, action="add", content="env is wsl", target="memory")
        user_read = await memory(ctx, action="read", target="user")
        mem_read = await memory(ctx, action="read", target="memory")
        assert "prefers terse" in user_read
        assert "prefers terse" not in mem_read
        assert "env is wsl" in mem_read
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_tool_rejects_duplicate(tmp_path, monkeypatch):
    from sophclaw.tools.memory import memory

    ctx, db = await _tool_ctx(tmp_path, monkeypatch)
    try:
        await memory(ctx, action="add", content="same fact", target="memory")
        out = await memory(ctx, action="add", content="same fact", target="memory")
        assert "duplicate" in out.lower()
        rows = await db.memory_list(1, target="memory")
        assert len(rows) == 1
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_tool_over_capacity_returns_consolidation_prompt(tmp_path, monkeypatch):
    from sophclaw.tools.memory import memory

    ctx, db = await _tool_ctx(tmp_path, monkeypatch)
    cfg = config_mod.get_config()
    cfg.memory_total_chars = 40  # 缩小便于触发
    try:
        await memory(ctx, action="add", content="x" * 35, target="memory")
        out = await memory(ctx, action="add", content="y" * 20, target="memory")
        # 不是死错：应带用量与现有条目，引导同轮整合
        assert "35/40" in out or "/40" in out
        assert "x" * 35 in out  # current_entries 列出现有条目
        assert "consolidate" in out.lower()
        # 未写入
        assert len(await db.memory_list(1, target="memory")) == 1
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_tool_target_isolation_in_capacity(tmp_path, monkeypatch):
    """user 段写满不应影响 memory 段的容量判断。"""
    from sophclaw.tools.memory import memory

    ctx, db = await _tool_ctx(tmp_path, monkeypatch)
    cfg = config_mod.get_config()
    cfg.user_total_chars = 30
    cfg.memory_total_chars = 2200
    try:
        await memory(ctx, action="add", content="u" * 25, target="user")
        # memory 段仍空，应能正常写入
        out = await memory(ctx, action="add", content="m" * 25, target="memory")
        assert "saved" in out.lower()
    finally:
        await db.close()
        config_mod.reset_config()


@pytest.mark.asyncio
async def test_tool_unknown_target_errors(tmp_path, monkeypatch):
    from sophclaw.tools.memory import memory

    ctx, db = await _tool_ctx(tmp_path, monkeypatch)
    try:
        out = await memory(ctx, action="add", content="x", target="bogus")
        assert "error" in out.lower() and "target" in out.lower()
    finally:
        await db.close()
        config_mod.reset_config()


# -- 单元 5：双段 system prompt 注入 ----------------------------------------


def _agent():
    from sophclaw.models import AgentDef

    return AgentDef(
        id=1, name="t", description="", system_prompt="You are helpful.",
        provider="test", model="m", tools=["memory"], skills=None,
    )


def test_prompt_renders_two_sections_with_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    cfg = config_mod.get_config()
    cfg.memory_total_chars = 2200
    cfg.user_total_chars = 1375
    from sophclaw.agent.prompt import build_system_prompt

    try:
        out = build_system_prompt(
            _agent(),
            memories={"memory": ["env is wsl"], "user": ["prefers terse"]},
        )
        assert "MEMORY" in out and "USER PROFILE" in out
        assert "env is wsl" in out
        assert "prefers terse" in out
        # usage 头：字符数/上限
        assert "/2200" in out
        assert "/1375" in out
    finally:
        config_mod.reset_config()


def test_prompt_empty_sections_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    from sophclaw.agent.prompt import build_system_prompt

    try:
        out = build_system_prompt(_agent(), memories={"memory": [], "user": []})
        # 两段都空时不应出现记忆标题
        assert "USER PROFILE" not in out
        assert "MEMORY (" not in out
    finally:
        config_mod.reset_config()


def test_prompt_one_section_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SOPHCLAW_DATA_DIR", str(tmp_path / "data"))
    config_mod.reset_config()
    from sophclaw.agent.prompt import build_system_prompt

    try:
        out = build_system_prompt(_agent(), memories={"memory": ["just a note"], "user": []})
        assert "just a note" in out
        assert "MEMORY" in out
        assert "USER PROFILE" not in out
    finally:
        config_mod.reset_config()
