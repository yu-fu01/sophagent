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
