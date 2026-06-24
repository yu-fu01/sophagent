"""SessionManager: per-session locks, global turn concurrency, stop support,
and per-session message queue for queuing input during active turns."""

from __future__ import annotations

import asyncio


class SessionManager:
    def __init__(self, max_concurrent_turns: int):
        self.semaphore = asyncio.Semaphore(max_concurrent_turns)
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._message_queues: dict[str, asyncio.Queue] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def is_busy(self, session_id: str) -> bool:
        lock = self._locks.get(session_id)
        task = self._tasks.get(session_id)
        return (lock is not None and lock.locked()) or (task is not None and not task.done())

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

    # ── Message queue ─────────────────────────────────────────────────────

    MAX_QUEUED = 3  # 最多允许排队消息数

    def queue_for(self, session_id: str) -> asyncio.Queue:
        """Get-or-create a per-session message queue for queued input."""
        return self._message_queues.setdefault(session_id, asyncio.Queue(maxsize=self.MAX_QUEUED))

    def try_put(self, session_id: str, content: str) -> bool:
        """尝试将消息加入排队队列，如果队列已满则返回 False。"""
        q = self.queue_for(session_id)
        try:
            q.put_nowait(content)
        except asyncio.QueueFull:
            return False
        return True

    def pending_count(self, session_id: str) -> int:
        """Number of messages waiting in the queue."""
        q = self._message_queues.get(session_id)
        return q.qsize() if q else 0

    def drain_queue(self, session_id: str) -> str | None:
        """Non-blocking pop the next queued message (None if empty)."""
        q = self._message_queues.get(session_id)
        if q and not q.empty():
            try:
                content = q.get_nowait()
                if q.empty():
                    self._message_queues.pop(session_id, None)
                return content
            except asyncio.QueueEmpty:
                return None
        return None

    def clear_queue(self, session_id: str) -> None:
        """Discard all queued messages for a session."""
        q = self._message_queues.get(session_id)
        if q:
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self._message_queues.pop(session_id, None)
