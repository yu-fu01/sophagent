"""Background cron ticker for sophclaw-agent.

Runs an asyncio background task that periodically checks for due cron jobs
and executes them through the agent's LLM pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from ..db_cron import get_due_cron_jobs, mark_cron_job_run, advance_next_run
from ..models import Message

log = logging.getLogger(__name__)


class CronTicker:
    """Background ticker that fires due cron jobs.

    Runs as a single asyncio task that sleeps between ticks. Each due job
    is dispatched as a separate task so one long-running job doesn't block
    the ticker or other jobs.
    """

    def __init__(self, db: Any, app_state: Any):
        self.db = db
        self.app_state = app_state
        self._task: asyncio.Task | None = None
        self._running_jobs: set[str] = set()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, interval: int = 60) -> None:
        """Start the background tick loop."""
        if self.is_running:
            log.warning("Cron ticker is already running")
            return
        self._task = asyncio.create_task(self._loop(interval))
        log.info("Cron ticker started (interval=%ds)", interval)

    async def stop(self) -> None:
        """Stop the background tick loop."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        log.info("Cron ticker stopped")

    async def _loop(self, interval: int) -> None:
        """Main tick loop."""
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Cron tick error")
            await asyncio.sleep(interval)

    async def _tick(self) -> None:
        """Check for due jobs and dispatch execution."""
        due = await get_due_cron_jobs(self.db)
        if not due:
            return

        log.info("Tick: %d job(s) due", len(due))

        for job in due:
            job_id = job["id"]
            if job_id in self._running_jobs:
                log.debug("Job '%s' already running, skipping", job_id)
                continue

            self._running_jobs.add(job_id)
            asyncio.create_task(self._execute_job(job))

    async def _execute_job(self, job: dict) -> None:
        """Execute one cron job: advance next_run, build runner, run prompt,
        collect output, save result."""
        job_id = job["id"]
        job_name = job.get("name") or job_id

        try:
            # 1. Advance next_run_at before execution (at-most-once)
            await advance_next_run(self.db, job_id)

            # 2. Resolve the agent definition
            agent_row = await self.db.get_agent(job["agent_id"])
            if agent_row is None:
                raise RuntimeError(f"Agent {job['agent_id']} not found")

            from ..models import AgentDef, Message

            agent = AgentDef.from_row(agent_row)

            # 3. Build the runner
            from ..agent.runtime import build_runner

            # Get the user's memory for context
            memories = []
            try:
                memories = [r["content"] for r in await self.db.memory_list(job["user_id"])]
            except Exception:
                pass

            runner = await build_runner(
                db=self.db,
                skill_store=self.app_state.skill_store,
                agent=agent,
                user_id=job["user_id"],
                history=[],  # cron jobs start fresh each run
                session_id=job.get("session_id"),
            )

            # 4. Run the prompt, collect final response
            prompt = job.get("prompt") or ""
            if not prompt:
                raise RuntimeError("Cron job has no prompt")

            log.info("Running cron job '%s' (ID: %s)", job_name, job_id)

            response_parts: list[str] = []
            async for ev in runner.run(prompt):
                if ev.get("type") == "text_delta":
                    response_parts.append(ev.get("text", ""))
                elif ev.get("type") == "error":
                    error_msg = ev.get("message", "Unknown error during execution")
                    await mark_cron_job_run(self.db, job_id, False, error_msg)
                    log.error("Cron job '%s' failed: %s", job_name, error_msg)
                    return

            final_response = "".join(response_parts).strip()

            output_doc = (
                f"# Cron Job: {job_name}\n\n"
                f"**Job ID:** {job_id}\n"
                f"**Prompt:** {prompt}\n\n"
                f"## Response\n\n{final_response}\n"
            )

            # 5. Push result to session if configured
            session_id = job.get("session_id")
            if session_id and final_response:
                try:
                    user_msg = Message(role="user", content=prompt)
                    assistant_msg = Message(role="assistant", content=final_response)
                    await self.db.append_messages(session_id, [user_msg, assistant_msg])
                    log.info("Pushed cron job '%s' output to session '%s'", job_name, session_id)
                except Exception as e:
                    log.warning("Failed to push cron job result to session '%s': %s", session_id, e)

            success = bool(final_response)
            error = None if success else "Agent produced empty response"

            await mark_cron_job_run(self.db, job_id, success, error, output_doc)
            log.info("Cron job '%s' completed %s", job_name, "successfully" if success else "with empty response")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Cron job '%s' failed with exception", job_name)
            await mark_cron_job_run(self.db, job_id, False, str(e))
        finally:
            self._running_jobs.discard(job_id)