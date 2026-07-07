"""定时任务（cron）测试：调度解析、DB CRUD、scheduler fire（text + agent 模式）。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from sophagent.agent.manager import SessionManager
from sophagent.config import reset_config
from sophagent.cron import jobs as cron_jobs
from sophagent.cron.scheduler import CronScheduler
from sophagent.db import Database
from sophagent.gateway.session_state import SessionRegistry
from sophagent.models import Message
from sophagent.tools.cron import cron as cron_tool
from sophagent.tools.registry import ToolContext


# ── 纯逻辑：parse / compute / describe ────────────────────────────────────

def test_parse_interval():
    s = cron_jobs.parse_schedule("every 30m")
    assert s == {"kind": "interval", "minutes": 30}
    assert cron_jobs.parse_schedule("every 2h") == {"kind": "interval", "minutes": 120}
    assert cron_jobs.parse_schedule("every 1d") == {"kind": "interval", "minutes": 1440}


def test_parse_cron():
    s = cron_jobs.parse_schedule("0 9 * * *")
    assert s == {"kind": "cron", "expr": "0 9 * * *"}


def test_parse_once_relative_and_absolute():
    s = cron_jobs.parse_schedule("30m")
    assert s["kind"] == "once"
    datetime.fromisoformat(s["run_at"])  # 解析得出来即可
    s2 = cron_jobs.parse_schedule("2026-06-25T14:00")
    assert s2 == {"kind": "once", "run_at": "2026-06-25T14:00:00"}


def test_parse_invalid():
    for bad in ["", "not a schedule", "1 2 3 4"]:
        with pytest.raises(ValueError):
            cron_jobs.parse_schedule(bad)


def test_compute_next_run_interval():
    s = {"kind": "interval", "minutes": 30}
    base = datetime(2026, 6, 25, 10, 0)
    assert cron_jobs.compute_next_run(s, base) == datetime(2026, 6, 25, 10, 30)


def test_compute_next_run_cron():
    s = {"kind": "cron", "expr": "0 9 * * *"}
    base = datetime(2026, 6, 25, 8, 0)
    assert cron_jobs.compute_next_run(s, base) == datetime(2026, 6, 25, 9, 0)


def test_describe_humanize():
    assert cron_jobs.describe_schedule({"kind": "interval", "minutes": 30}) == "每 30 分钟"
    assert cron_jobs.describe_schedule({"kind": "interval", "minutes": 120}) == "每 2 小时"
    assert cron_jobs.describe_schedule(cron_jobs.parse_schedule("0 9 * * *")) == "每天 09:00"
    assert cron_jobs.describe_schedule(cron_jobs.parse_schedule("0 * * * *")) == "每个整点"
    assert cron_jobs.describe_schedule(cron_jobs.parse_schedule("0 9 * * 1-5")) == "工作日 09:00"


# ── DB CRUD + scheduler fire ───────────────────────────────────────────────

async def _make_db(tmp_path) -> Database:
    reset_config()
    db = Database(tmp_path / "cron.db")
    await db.connect()
    await db.ensure_groups()
    uid = await db.create_user("u", "x", "user")
    await db.ensure_groups()  # 给新用户建个人组
    gid = (await db.get_owned_group(uid))["id"]
    agent_id = await db.create_agent(
        {"name": "a", "system_prompt": "x", "provider": "test", "model": "m",
         "tools": [], "skills": None, "max_iterations": 1, "temperature": None},
        uid, gid,
    )
    sid = uuid.uuid4().hex
    await db.create_session(sid, uid, agent_id, gid, "t")
    return db, sid, uid


async def test_create_and_list_job_via_db(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    sched = cron_jobs.parse_schedule("every 30m")
    next_iso = cron_jobs.compute_next_run(sched, datetime.now()).isoformat(timespec="seconds")
    jid = await db.create_cron_job({
        "id": "abc123", "session_id": sid, "user_id": uid, "name": "提醒吃饭",
        "prompt": "该吃饭啦", "mode": "text", "schedule": sched,
        "schedule_display": cron_jobs.describe_schedule(sched),
        "repeat_times": None, "next_run_at": next_iso,
    })
    assert jid == "abc123"
    rows = await db.list_cron_jobs_by_session(sid)
    assert len(rows) == 1
    j = cron_jobs.row_to_job(rows[0])
    assert j["name"] == "提醒吃饭" and j["mode"] == "text"
    await db.close()


async def test_text_mode_fire_appends_message(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    # 建一个已到期的 text 任务
    past = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    sched = {"kind": "interval", "minutes": 30}
    await db.create_cron_job({
        "id": "due1", "session_id": sid, "user_id": uid, "name": "运动",
        "prompt": "起来运动一下！", "mode": "text", "schedule": sched,
        "schedule_display": "每 30 分钟", "repeat_times": None, "next_run_at": past,
    })
    manager = SessionManager(32)
    registry = SessionRegistry(grace_seconds=60.0)
    sched_obj = CronScheduler(db, manager, None, registry, data_dir=tmp_path)
    n = await sched_obj.tick()
    assert n == 1
    msgs = await db.load_messages(sid)
    assert any("起来运动一下" in m.content for m in msgs)
    j = cron_jobs.row_to_job(await db.get_cron_job("due1"))
    assert j["last_status"] == "ok"
    assert j["state"] == "scheduled"          # recurring，未完成
    assert j["next_run_at"] != past           # 已推进（at-most-once）
    assert j["repeat_completed"] == 1
    await db.close()


async def test_text_mode_fire_mirrors_to_source_session(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    source_session = await db.get_session(sid)
    cron_gid = await db.get_or_create_cron_group(uid)
    cron_sid = uuid.uuid4().hex
    await db.create_session(cron_sid, uid, source_session["agent_id"], cron_gid, "吃饭提醒")
    past = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    await db.create_cron_job({
        "id": "mirror-text", "session_id": cron_sid, "source_session_id": sid,
        "user_id": uid, "name": "吃饭", "prompt": "该吃饭啦", "mode": "text",
        "schedule": {"kind": "interval", "minutes": 30},
        "schedule_display": "每 30 分钟", "repeat_times": None, "next_run_at": past,
    })
    sched_obj = CronScheduler(db, SessionManager(32), None,
                              SessionRegistry(grace_seconds=60.0), data_dir=tmp_path)
    assert await sched_obj.tick() == 1
    source_msgs = await db.load_messages(sid)
    assert any("定时任务: 吃饭" in m.content for m in source_msgs)
    assert any("该吃饭啦" in m.content for m in source_msgs)
    cron_msgs = await db.load_messages(cron_sid)
    assert any("该吃饭啦" in m.content for m in cron_msgs)
    await db.close()


async def test_once_job_completes_after_fire(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    sched = {"kind": "once", "run_at": past}
    await db.create_cron_job({
        "id": "once1", "session_id": sid, "user_id": uid, "name": "一次",
        "prompt": "boom", "mode": "text", "schedule": sched,
        "schedule_display": "一次性", "repeat_times": None, "next_run_at": past,
    })
    sched_obj = CronScheduler(db, SessionManager(32), None,
                              SessionRegistry(grace_seconds=60.0), data_dir=tmp_path)
    await sched_obj.tick()
    j = cron_jobs.row_to_job(await db.get_cron_job("once1"))
    assert j["state"] == "completed"
    assert j["next_run_at"] is None
    await db.close()


# ── REQ1/REQ2: 独立 session + cron 组 ───────────────────────────────────────

def _ctx(db, sid, uid):
    return ToolContext(user_id=uid, workspace=db.path.parent, agent=None, db=db, session_id=sid)


async def test_get_or_create_cron_group_idempotent(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    g1 = await db.get_or_create_cron_group(uid)
    g2 = await db.get_or_create_cron_group(uid)
    assert g1 == g2  # 幂等
    row = await db.get_group(g1)
    assert row["is_cron_group"] == 1
    assert row["owner_id"] == uid
    await db.close()


async def test_cron_create_makes_dedicated_session_in_cron_group(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    out = await cron_tool(_ctx(db, sid, uid), "create", name="提醒",
                          prompt="该吃饭", schedule="every 30m", mode="text")
    assert "已创建" in out
    # 任务不再绑在发起的聊天 session 上
    assert len(await db.list_cron_jobs_by_session(sid)) == 0
    jobs = await db.list_cron_jobs(uid)
    assert len(jobs) == 1
    assert jobs[0]["source_session_id"] == sid
    job_sid = jobs[0]["session_id"]
    assert job_sid != sid                       # REQ1：新 session
    sess = await db.get_session(job_sid)
    cron_gid = await db.get_or_create_cron_group(uid)
    assert sess["group_id"] == cron_gid         # REQ2：归 cron 组
    assert sess["title"] == "提醒"               # 标题=任务名
    await db.close()


async def test_each_cron_job_gets_its_own_session(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    await cron_tool(_ctx(db, sid, uid), "create", name="A", prompt="a", schedule="every 30m", mode="text")
    await cron_tool(_ctx(db, sid, uid), "create", name="B", prompt="b", schedule="every 1h", mode="text")
    jobs = await db.list_cron_jobs(uid)
    sids = {j["session_id"] for j in jobs}
    assert len(jobs) == 2 and len(sids) == 2    # REQ2：每任务一个独立 session
    await db.close()


async def test_cron_list_aggregates_by_user_across_sessions(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    await cron_tool(_ctx(db, sid, uid), "create", name="A", prompt="a", schedule="every 30m", mode="text")
    await cron_tool(_ctx(db, sid, uid), "create", name="B", prompt="b", schedule="every 1h", mode="text")
    # 从任意一个聊天 session 执行 list，应列出该用户全部任务（跨 session）
    out = await cron_tool(_ctx(db, sid, uid), "list")
    assert "A" in out and "B" in out
    await db.close()


async def test_cron_delete_removes_job_and_its_session(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    await cron_tool(_ctx(db, sid, uid), "create", name="A", prompt="a", schedule="every 30m", mode="text")
    job = (await db.list_cron_jobs(uid))[0]
    jid, job_sid = job["id"], job["session_id"]
    out = await cron_tool(_ctx(db, sid, uid), "delete", job_id=jid)
    assert "已删除" in out
    assert await db.get_cron_job(jid) is None        # 任务没了
    assert await db.get_session(job_sid) is None      # 其专属 session 也没了
    await db.close()


async def test_list_sessions_marks_cron_sessions(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    await cron_tool(_ctx(db, sid, uid), "create", name="A", prompt="a", schedule="every 30m", mode="text")
    rows = {r["id"]: r for r in await db.list_sessions(uid)}
    assert rows[sid]["is_cron"] == 0                  # 普通聊天 session
    job_sid = (await db.list_cron_jobs(uid))[0]["session_id"]
    assert rows[job_sid]["is_cron"] == 1              # cron session
    await db.close()


# ── REQ3: 消息时间戳 ─────────────────────────────────────────────────────────

async def test_load_messages_with_ids_returns_created_at(tmp_path):
    db, sid, uid = await _make_db(tmp_path)
    await db.append_messages(sid, [Message(role="user", content="hi")])
    rows = await db.load_messages_with_ids(sid)
    assert len(rows) == 1
    mid, msg, created_at = rows[0]                     # 三元组
    assert msg.content == "hi"
    assert isinstance(created_at, str) and created_at  # 非空 ISO 时间
    await db.close()


# ── interval 锚定到计划时刻，避免漂移（修复"2分钟变3分钟"）──────────────────

def test_next_interval_run_anchors_to_schedule():
    # 锚定到 scheduled，而非 now（now 比 scheduled 晚 30s 也不影响结果）
    scheduled = datetime(2026, 6, 26, 10, 0, 0)
    now = datetime(2026, 6, 26, 10, 0, 30)
    assert cron_jobs.next_interval_run(2, scheduled, now) == datetime(2026, 6, 26, 10, 2, 0)


def test_next_interval_run_catches_up_past_now():
    # 落后多个周期时，补齐到严格晚于 now 的下一个计划点
    scheduled = datetime(2026, 6, 26, 10, 0, 0)
    now = datetime(2026, 6, 26, 10, 5, 10)
    assert cron_jobs.next_interval_run(2, scheduled, now) == datetime(2026, 6, 26, 10, 6, 0)


async def test_scheduler_interval_next_run_anchored_not_drifting(tmp_path):
    """触发后 next_run 应等于 计划时刻+interval（锚定），而非 实际触发时刻+interval。"""
    db, sid, uid = await _make_db(tmp_path)
    scheduled = datetime.now().replace(microsecond=0) - timedelta(seconds=30)  # 30s 前到期
    await db.create_cron_job({
        "id": "drift1", "session_id": sid, "user_id": uid, "name": "喝水",
        "prompt": "喝水", "mode": "text", "schedule": {"kind": "interval", "minutes": 2},
        "schedule_display": "每 2 分钟", "repeat_times": None,
        "next_run_at": scheduled.isoformat(timespec="seconds"),
    })
    sch = CronScheduler(db, SessionManager(32), None,
                        SessionRegistry(grace_seconds=60.0), data_dir=tmp_path)
    await sch.tick()
    j = cron_jobs.row_to_job(await db.get_cron_job("drift1"))
    expected = (scheduled + timedelta(minutes=2)).isoformat(timespec="seconds")
    assert j["next_run_at"] == expected   # 不含"实际触发比计划晚的那 30s"
    await db.close()


# ── repeat<=0 视为无限（修复"循环任务只触发一次"）─────────────────────────

async def test_recurring_nonpositive_repeat_keeps_firing(tmp_path):
    """repeat_times=-1（LLM 表达'无限'）不应在首次触发后置 completed。"""
    db, sid, uid = await _make_db(tmp_path)
    past = (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    await db.create_cron_job({
        "id": "neg1", "session_id": sid, "user_id": uid, "name": "喝水",
        "prompt": "喝水", "mode": "text", "schedule": {"kind": "interval", "minutes": 30},
        "schedule_display": "每 30 分钟", "repeat_times": -1, "next_run_at": past,
    })
    sch = CronScheduler(db, SessionManager(32), None,
                        SessionRegistry(grace_seconds=60.0), data_dir=tmp_path)
    await sch.tick()
    j = cron_jobs.row_to_job(await db.get_cron_job("neg1"))
    assert j["state"] == "scheduled"      # 不该 completed
    assert j["next_run_at"] != past        # 已推进，会再触发
    await db.close()


async def test_cron_create_normalizes_nonpositive_repeat(tmp_path):
    """工具层把 repeat<=0 规范化为无限（None）。"""
    db, sid, uid = await _make_db(tmp_path)
    await cron_tool(_ctx(db, sid, uid), "create", name="x", prompt="p",
                    schedule="every 30m", mode="text", repeat=-1)
    job = (await db.list_cron_jobs(uid))[0]
    assert job["repeat_times"] is None
    await db.close()


async def test_agent_overrun_skips_while_busy_then_fires_when_free(tmp_path, monkeypatch):
    """执行时长 > 间隔：上一轮还在跑时到期的拍被跳过（不并发、不推进、不 mark），
    会话空闲后才补触发一次，且 next_run 跳到未来（错过的时段合并、不积压）。"""
    db, sid, uid = await _make_db(tmp_path)

    started = 0  # cron 实际派发 run_turns 的次数

    async def fake_run_turns(*, session_id, user_input, db, manager, skill_store, user_id):
        nonlocal started
        started += 1
        yield {"type": "done"}

    monkeypatch.setattr("sophagent.cron.scheduler.run_turns", fake_run_turns)

    sch = CronScheduler(db, SessionManager(32), None,
                        SessionRegistry(grace_seconds=60.0), data_dir=tmp_path)

    # 模拟该会话正在跑一个"长 turn"（卡在 gate 上，迟迟不结束）
    gate = asyncio.Event()

    async def long_turn():
        await gate.wait()
        yield {"type": "done"}

    state = sch.registry.get_or_create(sid, uid)
    busy_task = state.start_turn(lambda: long_turn())
    sch.manager.register_task(sid, busy_task)
    await asyncio.sleep(0.02)
    assert sch.manager.is_busy(sid) is True

    # 一个已到期的 agent 定时任务（绑这个忙碌会话）
    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    await db.create_cron_job({
        "id": "ov1", "session_id": sid, "user_id": uid, "name": "长任务",
        "prompt": "喝水", "mode": "agent", "schedule": {"kind": "interval", "minutes": 1},
        "schedule_display": "每 1 分钟", "repeat_times": None, "next_run_at": past,
    })

    # tick：会话忙 → 跳过（不并发、不 fire、不推进 next_run、不 mark）
    n = await sch.tick()
    await asyncio.sleep(0.02)
    assert n == 0 and started == 0
    j = cron_jobs.row_to_job(await db.get_cron_job("ov1"))
    assert j["next_run_at"] == past and j["last_run_at"] is None

    # 长 turn 结束 → 会话空闲
    gate.set()
    await asyncio.sleep(0.05)
    assert sch.manager.is_busy(sid) is False

    # 再 tick：空闲 → 补触发一次；next_run 跳到未来（合并掉错过的时段）
    await sch.tick()
    await asyncio.sleep(0.05)
    assert started == 1
    nxt = cron_jobs.row_to_job(await db.get_cron_job("ov1"))["next_run_at"]
    assert datetime.fromisoformat(nxt) > datetime.now() - timedelta(seconds=2)
    await db.close()


async def test_agent_mode_fire_runs_turn_and_marks(tmp_path, monkeypatch):
    db, sid, uid = await _make_db(tmp_path)

    # patch run_turns：假跑一轮，产出一条 text_delta + done
    async def fake_run_turns(*, session_id, user_input, db, manager, skill_store, user_id):
        yield {"type": "text_delta", "text": f"cron-reply:{user_input}"}
        yield {"type": "done"}

    monkeypatch.setattr("sophagent.cron.scheduler.run_turns", fake_run_turns)

    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    sched = {"kind": "interval", "minutes": 60}
    await db.create_cron_job({
        "id": "ag1", "session_id": sid, "user_id": uid, "name": "agent任务",
        "prompt": "提醒吃饭", "mode": "agent", "schedule": sched,
        "schedule_display": "每 60 分钟", "repeat_times": None, "next_run_at": past,
    })
    manager = SessionManager(32)
    registry = SessionRegistry(grace_seconds=60.0)
    sched_obj = CronScheduler(db, manager, None, registry, data_dir=tmp_path)
    await sched_obj.tick()
    # fire 起的是后台 task，等它跑完 + done 回调里的 mark 落地
    await asyncio.sleep(0.3)
    j = cron_jobs.row_to_job(await db.get_cron_job("ag1"))
    assert j["last_status"] == "ok"
    assert j["next_run_at"] != past           # 已推进
    assert j["repeat_completed"] == 1
    await db.close()


async def test_agent_mode_fire_mirrors_final_reply_to_source_session(tmp_path, monkeypatch):
    db, sid, uid = await _make_db(tmp_path)
    source_session = await db.get_session(sid)
    cron_gid = await db.get_or_create_cron_group(uid)
    cron_sid = uuid.uuid4().hex
    await db.create_session(cron_sid, uid, source_session["agent_id"], cron_gid, "agent提醒")

    async def fake_run_turns(*, session_id, user_input, db, manager, skill_store, user_id):
        await db.append_messages(session_id, [
            Message(role="user", content=user_input),
            Message(role="assistant", content=f"cron-agent-reply:{user_input}"),
        ])
        yield {"type": "text_delta", "text": f"cron-agent-reply:{user_input}"}
        yield {"type": "done"}

    monkeypatch.setattr("sophagent.cron.scheduler.run_turns", fake_run_turns)

    past = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    await db.create_cron_job({
        "id": "mirror-agent", "session_id": cron_sid, "source_session_id": sid,
        "user_id": uid, "name": "agent提醒", "prompt": "提醒吃饭", "mode": "agent",
        "schedule": {"kind": "interval", "minutes": 60},
        "schedule_display": "每 60 分钟", "repeat_times": None, "next_run_at": past,
    })
    sched_obj = CronScheduler(db, SessionManager(32), None,
                              SessionRegistry(grace_seconds=60.0), data_dir=tmp_path)
    await sched_obj.tick()
    await asyncio.sleep(0.3)
    source_msgs = await db.load_messages(sid)
    assert any("定时任务: agent提醒" in m.content for m in source_msgs)
    assert any("cron-agent-reply:提醒吃饭" in m.content for m in source_msgs)
    await db.close()
