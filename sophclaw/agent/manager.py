"""SessionManager: per-session locks, global turn concurrency, stop support."""

from __future__ import annotations

import asyncio


class SessionManager:
    def __init__(self, max_concurrent_turns: int):
        self.semaphore = asyncio.Semaphore(max_concurrent_turns)
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def is_busy(self, session_id: str) -> bool:
        lock = self._locks.get(session_id)
        return lock is not None and lock.locked()

    def register_task(self, session_id: str, task: asyncio.Task) -> None:
        self._tasks[session_id] = task
        task.add_done_callback(lambda _: self._cleanup(session_id, task))

    def _cleanup(self, session_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(session_id) is task:
            del self._tasks[session_id]
        lock = self._locks.get(session_id)
        if lock is not None and not lock.locked():
            del self._locks[session_id]

    def stop(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            return True
        return False
