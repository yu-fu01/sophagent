"""CronScheduler：60s tick 循环 + 到期 fire。

仿 hermes 的设计，但适配 sophagent 的 async 栈：

* 单 asyncio 后台任务，每 ``cron_tick_interval_seconds`` 秒一轮（默认 60s）；
* 到期 job 复用共享 turn 核心 ``run_turns``（与 WS/REST/IM 同路径），事件经
  ``SessionState.on_event`` 推给在线 WS 客户端；客户端不在线则 turn 仍跑完落库，
  重开 session 可见；
* at-most-once：dispatch 后立即把 ``next_run_at`` 推进到下一未来点（recurring）
  或置空（once），崩溃重启不会连发；
* in-flight 串行：agent 模式若该 session 正有 turn 在跑，本 tick 跳过、不推进，
  下 tick 重试——既避免 clobber 在跑 turn，又保证 session 空闲后补发一次（不爆裂）；
* text 模式不调 LLM，直接把 prompt 文本当消息落库 + （session 空闲时）推在线客户端。

时间全程本地 naive（见 ``cron/jobs.py``）。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..agent.turn import run_turns
from ..config import get_config
from ..models import Message
from .jobs import compute_next_run, fmt_local, next_interval_run, row_to_job

log = logging.getLogger(__name__)


class CronScheduler:
    def __init__(self, db, manager, skill_store, registry, *, data_dir: Path | None = None):
        self.db = db
        self.manager = manager
        self.skill_store = skill_store
        self.registry = registry
        cfg = get_config()
        self.tick_interval = cfg.cron_tick_interval_seconds
        self._cron_dir = (data_dir or cfg.data_dir) / "cron"
        self._cron_dir.mkdir(parents=True, exist_ok=True)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="cron-scheduler")
        log.info("cron scheduler started (tick=%ss)", self.tick_interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
                self._write_heartbeat(success=True)
            except Exception:  # noqa: BLE001 — ticker 绝不能被静默杀死
                log.exception("cron tick crashed")
                self._write_heartbeat(success=False)
            # 用 Event.wait 代替 sleep，stop 能立即唤醒
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_interval)
            except asyncio.TimeoutError:
                pass

    # ── tick ───────────────────────────────────────────────────────────────

    async def tick(self) -> int:
        """跑一轮：取出到期 job，逐个 fire。返回本轮 fire 的 job 数。"""
        now_iso = datetime.now().isoformat(timespec="seconds")
        due = await self.db.list_due_cron_jobs(now_iso)
        if not due:
            return 0
        n = 0
        for row in due:
            try:
                fired = await self._fire(row)
                if fired:
                    n += 1
            except Exception:
                log.exception("cron fire dispatch failed job=%s", row["id"] if "id" in row.keys() else "?")
        return n

    # ── fire ───────────────────────────────────────────────────────────────

    async def _fire(self, row: Any) -> bool:
        """调度一个到期 job。返回是否实际 dispatch（busy 跳过返回 False）。"""
        job = row_to_job(row)
        job_id = job["id"]
        session_id = job["session_id"]
        user_id = job["user_id"]
        schedule = job.get("schedule") or {}
        kind = schedule.get("kind")
        now = datetime.now()
        mode = job.get("mode", "agent") or "agent"

        # 校验 session/agent 仍在
        session = await self.db.get_session(session_id)
        if session is None:
            await self._mark(job, success=False, error="session 已删除", now=now)
            return True
        agent_row = await self.db.get_agent(session["agent_id"])
        if agent_row is None:
            await self._mark(job, success=False, error="agent 已删除", now=now)
            return True

        # at-most-once：算下一运行点（recurring 推进；once 置空）
        if kind == "once":
            next_run = None
        else:
            try:
                if kind == "interval":
                    # 锚定到计划时刻（job 当前 next_run_at），无漂移；缺失则退回 now
                    anchor = (datetime.fromisoformat(job["next_run_at"])
                              if job.get("next_run_at") else now)
                    next_run = next_interval_run(schedule["minutes"], anchor, now)
                else:
                    next_run = compute_next_run(schedule, now)
            except Exception as e:
                await self._mark(job, success=False, error=f"调度计算失败: {e}", now=now)
                return True
        next_run_iso = next_run.isoformat(timespec="seconds") if next_run else None

        if mode == "text":
            # text 模式：同步落库 + 推进 + mark；session 空闲才推在线客户端
            await self._fire_text(job, session_id, not self.manager.is_busy(session_id))
            await self.db.advance_cron_next_run(job_id, next_run_iso)
            repeat_new = (job.get("repeat_completed") or 0) + 1
            repeat_times = job.get("repeat_times")
            # repeat_times>0 才是有限次数；None/<=0 视为无限（避免循环任务只触发一次）
            completed_after = kind == "once" or (
                repeat_times is not None and repeat_times > 0 and repeat_new >= repeat_times
            )
            await self._mark(job, success=True, error=None, now=now,
                             next_run_iso=next_run_iso, completed=completed_after,
                             repeat_completed=repeat_new)
            return True

        # agent 模式：session 忙则跳过（不推进、不 mark），下 tick 重试
        if self.manager.is_busy(session_id):
            log.info("cron skip busy session job=%s session=%s", job_id, session_id)
            return False

        state = self.registry.get_or_create(session_id, user_id)
        task = state.start_turn(
            functools.partial(
                run_turns,
                session_id=session_id,
                user_input=job.get("prompt", ""),
                db=self.db,
                manager=self.manager,
                skill_store=self.skill_store,
                user_id=user_id,
            )
        )
        self.manager.register_task(session_id, task)

        # dispatch 成功后立即推进（at-most-once）；done 后再 mark 状态
        await self.db.advance_cron_next_run(job_id, next_run_iso)

        repeat_new = (job.get("repeat_completed") or 0) + 1
        repeat_times = job.get("repeat_times")
        # repeat_times>0 才是有限次数；None/<=0 视为无限（避免循环任务只触发一次）
        completed_after = kind == "once" or (
            repeat_times is not None and repeat_times > 0 and repeat_new >= repeat_times
        )

        def _on_done(t: asyncio.Task) -> None:
            success = not t.cancelled() and t.exception() is None
            err = str(t.exception()) if (not success and t.exception() is not None) else None
            # 调度 mark 到 loop 上（done 回调里不能直接 await）
            asyncio.create_task(self._mark(job, success=success, error=err, now=datetime.now(),
                                           next_run_iso=next_run_iso, completed=completed_after,
                                           repeat_completed=repeat_new))

        task.add_done_callback(_on_done)
        log.info("cron fired job=%s session=%s mode=agent next=%s",
                 job_id, session_id, fmt_local(next_run_iso))
        return True

    async def _fire_text(self, job: dict, session_id: str, emit_live: bool) -> None:
        """text 模式：把 prompt 文本当 assistant 消息落库；空闲时推在线客户端。"""
        name = job.get("name", "")
        prompt = job.get("prompt", "")
        trigger = f"⏰ [定时任务: {name}]" if name else "⏰ [定时任务]"
        await self.db.append_messages(session_id, [
            Message(role="user", content=trigger),
            Message(role="assistant", content=prompt),
        ])
        if emit_live:
            state = self.registry.get(session_id)
            if state is not None:
                try:
                    await state.on_event({"type": "text_delta", "text": prompt})
                    await state.on_event({"type": "done"})
                    await state.finish_turn()
                    # text 模式不走 _runner，没有它的收尾 settled 帧；显式补发一个，
                    # 让前端（含因本轮服务端发起而懒建的 turn）能正常收尾、退出 streaming。
                    await state.on_event({"type": "settled"})
                except Exception:
                    log.exception("cron text live-emit failed session=%s", session_id)

    async def _mark(self, job: dict, *, success: bool, error: str | None, now: datetime,
                    next_run_iso: str | None = None, completed: bool = False,
                    repeat_completed: int | None = None) -> None:
        try:
            await self.db.mark_cron_run(
                job["id"], success=success, error=error, next_run_at=next_run_iso,
                completed=completed, repeat_completed=repeat_completed,
            )
        except Exception:
            log.exception("cron mark_run failed job=%s", job.get("id"))

    # ── heartbeat ──────────────────────────────────────────────────────────

    def _write_heartbeat(self, *, success: bool) -> None:
        """每轮写 epoch；仅成功轮写 last_success。供后续 cron status 区分
        「线程死了」vs「活着但每轮失败」（对齐 hermes #32612）。"""
        ts = str(int(time.time()))
        try:
            (self._cron_dir / "ticker_heartbeat").write_text(ts, encoding="utf-8")
            if success:
                (self._cron_dir / "ticker_last_success").write_text(ts, encoding="utf-8")
        except Exception:
            log.debug("cron heartbeat write failed", exc_info=True)
