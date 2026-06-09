import asyncio
import logging
import time

import aiosqlite

from .models import Task, TaskStatus

logger = logging.getLogger(__name__)


class TaskQueue:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._notify = asyncio.Event()

    async def enqueue(self, task: Task) -> int:
        now = int(time.time())
        async with self._conn.execute(
            """INSERT INTO tasks (message_id, chat_id, user_id, content, status, priority, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task.message_id, task.chat_id, task.user_id, task.content,
             TaskStatus.PENDING.value, task.priority, now),
        ) as cur:
            task_id = cur.lastrowid
        await self._conn.commit()
        self._notify.set()
        logger.info(
            "Task %d enqueued chat=%d user=%d priority=%d len=%d",
            task_id, task.chat_id, task.user_id, task.priority, len(task.content),
        )
        return task_id  # type: ignore[return-value]

    async def next_pending(self, user_id: int | None = None) -> Task | None:
        query = """SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC, created_at ASC LIMIT 1"""
        params: tuple = (TaskStatus.PENDING.value,)
        if user_id is not None:
            query = """SELECT * FROM tasks WHERE status = ? AND user_id = ?
                       ORDER BY priority DESC, created_at ASC LIMIT 1"""
            params = (TaskStatus.PENDING.value, user_id)
        async with self._conn.execute(query, params) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def claim(self, task_id: int, agent_id: str) -> bool:
        now = int(time.time())
        async with self._conn.execute(
            """UPDATE tasks SET status = ?, agent_id = ?, started_at = ?
               WHERE id = ? AND status = ?""",
            (TaskStatus.IN_PROGRESS.value, agent_id, now, task_id, TaskStatus.PENDING.value),
        ) as cur:
            updated = cur.rowcount
        await self._conn.commit()
        if updated:
            logger.debug("Task %d claimed by agent %s", task_id, agent_id)
        return updated > 0

    async def complete(self, task_id: int, result: str) -> None:
        now = int(time.time())
        await self._conn.execute(
            """UPDATE tasks SET status = ?, result = ?, completed_at = ? WHERE id = ?""",
            (TaskStatus.DONE.value, result, now, task_id),
        )
        await self._conn.commit()
        logger.info("Task %d done result_len=%d", task_id, len(result))

    async def fail(self, task_id: int, error: str) -> None:
        now = int(time.time())
        await self._conn.execute(
            """UPDATE tasks SET status = ?, error = ?, completed_at = ? WHERE id = ?""",
            (TaskStatus.FAILED.value, error, now, task_id),
        )
        await self._conn.commit()
        logger.warning("Task %d failed: %s", task_id, error)

    async def cancel(self, task_id: int) -> None:
        await self._conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ? AND status IN (?, ?)",
            (TaskStatus.CANCELLED.value, task_id, TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value),
        )
        await self._conn.commit()

    async def active_count(self, user_id: int | None = None) -> int:
        if user_id is not None:
            async with self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = ? AND user_id = ?",
                (TaskStatus.IN_PROGRESS.value, user_id),
            ) as cur:
                row = await cur.fetchone()
        else:
            async with self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = ?",
                (TaskStatus.IN_PROGRESS.value,),
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else 0

    async def wait_for_work(self, timeout: float = 30.0) -> None:
        self._notify.clear()
        try:
            await asyncio.wait_for(self._notify.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


def _row_to_task(row: aiosqlite.Row) -> Task:
    return Task(
        id=row["id"],
        message_id=row["message_id"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        content=row["content"],
        status=TaskStatus(row["status"]),
        priority=row["priority"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        agent_id=row["agent_id"],
        result=row["result"],
        error=row["error"],
        context_tokens=row["context_tokens"] or 0,
    )
