import asyncio
import logging
import time

import aiosqlite

from .models import AgentClass, KanbanTask, Lane

logger = logging.getLogger(__name__)


class KanbanBoard:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._notify = asyncio.Event()

    async def push(self, task: KanbanTask) -> int:
        now = int(time.time())
        async with self._conn.execute(
            """INSERT INTO kanban_tasks
               (chat_id, user_id, content, lane, priority, created_at, message_id, parent_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (task.chat_id, task.user_id, task.content, Lane.BACKLOG.value,
             task.priority, now, task.message_id, task.parent_id),
        ) as cur:
            task_id = cur.lastrowid
        await self._conn.commit()
        self._notify.set()
        logger.info(
            "Kanban push id=%d chat=%d user=%d priority=%d len=%d",
            task_id, task.chat_id, task.user_id, task.priority, len(task.content),
        )
        return task_id  # type: ignore[return-value]

    async def assign(self, task_id: int, agent_class: AgentClass) -> bool:
        now = int(time.time())
        async with self._conn.execute(
            """UPDATE kanban_tasks SET lane = ?, agent_class = ?, assigned_at = ?
               WHERE id = ? AND lane = ?""",
            (Lane.ASSIGNED.value, agent_class.value, now, task_id, Lane.BACKLOG.value),
        ) as cur:
            updated = cur.rowcount
        await self._conn.commit()
        if updated:
            logger.info("Kanban assign id=%d class=%s", task_id, agent_class.value)
        return updated > 0

    async def start(self, task_id: int, agent_id: str) -> bool:
        now = int(time.time())
        async with self._conn.execute(
            """UPDATE kanban_tasks SET lane = ?, agent_id = ?, started_at = ?
               WHERE id = ? AND lane = ?""",
            (Lane.IN_PROGRESS.value, agent_id, now, task_id, Lane.ASSIGNED.value),
        ) as cur:
            updated = cur.rowcount
        await self._conn.commit()
        if updated:
            logger.info("Kanban start id=%d agent=%s", task_id, agent_id)
        return updated > 0

    async def review(self, task_id: int) -> None:
        await self._conn.execute(
            "UPDATE kanban_tasks SET lane = ? WHERE id = ?",
            (Lane.REVIEW.value, task_id),
        )
        await self._conn.commit()

    async def complete(self, task_id: int, result: str) -> None:
        now = int(time.time())
        await self._conn.execute(
            "UPDATE kanban_tasks SET lane = ?, result = ?, completed_at = ? WHERE id = ?",
            (Lane.DONE.value, result, now, task_id),
        )
        await self._conn.commit()
        logger.info("Kanban done id=%d result_len=%d", task_id, len(result))

    async def fail(self, task_id: int, error: str) -> None:
        now = int(time.time())
        await self._conn.execute(
            "UPDATE kanban_tasks SET lane = ?, error = ?, completed_at = ? WHERE id = ?",
            (Lane.FAILED.value, error, now, task_id),
        )
        await self._conn.commit()
        logger.warning("Kanban fail id=%d error=%s", task_id, error[:100])

    async def block(self, task_id: int, reason: str) -> None:
        await self._conn.execute(
            "UPDATE kanban_tasks SET lane = ?, error = ? WHERE id = ?",
            (Lane.BLOCKED.value, reason, task_id),
        )
        await self._conn.commit()
        logger.warning("Kanban block id=%d reason=%s", task_id, reason[:100])

    async def requeue(self, task_id: int) -> None:
        """Return a rate-limited IN_PROGRESS task to ASSIGNED for retry."""
        async with self._conn.execute(
            """UPDATE kanban_tasks SET lane = ?, started_at = NULL, agent_id = NULL
               WHERE id = ? AND lane = ?""",
            (Lane.ASSIGNED.value, task_id, Lane.IN_PROGRESS.value),
        ) as cur:
            updated = cur.rowcount
        await self._conn.commit()
        if updated:
            self._notify.set()
            logger.info("Kanban requeue id=%d", task_id)

    async def next_backlog(self) -> KanbanTask | None:
        async with self._conn.execute(
            """SELECT * FROM kanban_tasks WHERE lane = ? AND parent_id IS NULL
               ORDER BY priority DESC, created_at ASC LIMIT 1""",
            (Lane.BACKLOG.value,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def next_assigned(self, agent_class: AgentClass) -> KanbanTask | None:
        async with self._conn.execute(
            """SELECT * FROM kanban_tasks WHERE lane = ? AND agent_class = ?
               ORDER BY priority DESC, assigned_at ASC LIMIT 1""",
            (Lane.ASSIGNED.value, agent_class.value),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def get(self, task_id: int) -> KanbanTask | None:
        async with self._conn.execute(
            "SELECT * FROM kanban_tasks WHERE id = ?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def subtasks(self, parent_id: int) -> list[KanbanTask]:
        async with self._conn.execute(
            "SELECT * FROM kanban_tasks WHERE parent_id = ? ORDER BY created_at ASC",
            (parent_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def count_by_lane(self) -> dict[str, int]:
        async with self._conn.execute(
            "SELECT lane, COUNT(*) as n FROM kanban_tasks GROUP BY lane"
        ) as cur:
            rows = await cur.fetchall()
        return {r["lane"]: r["n"] for r in rows}

    async def list_lane(self, lane: Lane, limit: int = 50) -> list[KanbanTask]:
        async with self._conn.execute(
            "SELECT * FROM kanban_tasks WHERE lane = ? ORDER BY created_at DESC LIMIT ?",
            (lane.value, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def list_all(self, limit: int = 100) -> list[KanbanTask]:
        async with self._conn.execute(
            "SELECT * FROM kanban_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]

    async def push_child(self, task: KanbanTask, agent_class: AgentClass) -> int:
        """Insert child task directly into ASSIGNED lane, bypassing controller routing."""
        now = int(time.time())
        async with self._conn.execute(
            """INSERT INTO kanban_tasks
               (chat_id, user_id, content, lane, agent_class, priority,
                created_at, message_id, parent_id, assigned_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task.chat_id, task.user_id, task.content, Lane.ASSIGNED.value,
             agent_class.value, task.priority, now, task.message_id, task.parent_id, now),
        ) as cur:
            task_id = cur.lastrowid
        await self._conn.commit()
        self._notify.set()
        logger.info(
            "Kanban push_child id=%d parent=%d class=%s",
            task_id, task.parent_id, agent_class.value,
        )
        return task_id  # type: ignore[return-value]

    async def await_children(self, parent_id: int, child_ids: list[int]) -> list[KanbanTask]:
        """Poll until all specified children reach a terminal lane (done/failed/blocked)."""
        terminal = {Lane.DONE, Lane.FAILED, Lane.BLOCKED}
        while True:
            children = await self.subtasks(parent_id)
            done_ids = {c.id for c in children if c.lane in terminal}
            if set(child_ids) <= done_ids:
                return children
            await asyncio.sleep(2.0)

    async def next_failed(self, exclude_ids: "set[int] | None" = None) -> "KanbanTask | None":
        """Return oldest FAILED task not in exclude_ids (already-maxed recoveries)."""
        if exclude_ids:
            placeholders = ",".join("?" * len(exclude_ids))
            query = (
                f"SELECT * FROM kanban_tasks WHERE lane = ? AND id NOT IN ({placeholders})"
                " AND parent_id IS NULL ORDER BY completed_at ASC LIMIT 1"
            )
            params = [Lane.FAILED.value, *sorted(exclude_ids)]
        else:
            query = (
                "SELECT * FROM kanban_tasks WHERE lane = ? AND parent_id IS NULL"
                " ORDER BY completed_at ASC LIMIT 1"
            )
            params = [Lane.FAILED.value]
        async with self._conn.execute(query, params) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def recover(self, task_id: int, content: str | None = None) -> bool:
        """Move a FAILED task back to BACKLOG for retry. Optionally update content."""
        now = int(time.time())
        if content is not None:
            async with self._conn.execute(
                """UPDATE kanban_tasks SET lane = ?, error = NULL, content = ?,
                   agent_class = NULL, agent_id = NULL, started_at = NULL,
                   assigned_at = NULL, completed_at = NULL
                   WHERE id = ? AND lane = ?""",
                (Lane.BACKLOG.value, content, task_id, Lane.FAILED.value),
            ) as cur:
                updated = cur.rowcount
        else:
            async with self._conn.execute(
                """UPDATE kanban_tasks SET lane = ?, error = NULL,
                   agent_class = NULL, agent_id = NULL, started_at = NULL,
                   assigned_at = NULL, completed_at = NULL
                   WHERE id = ? AND lane = ?""",
                (Lane.BACKLOG.value, task_id, Lane.FAILED.value),
            ) as cur:
                updated = cur.rowcount
        await self._conn.commit()
        if updated:
            self._notify.set()
            logger.info("Kanban recover id=%d content_updated=%s", task_id, content is not None)
        return updated > 0

    async def wait_for_work(self, timeout: float = 30.0) -> None:
        self._notify.clear()
        try:
            await asyncio.wait_for(self._notify.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass


def _row_to_task(row: aiosqlite.Row) -> KanbanTask:
    return KanbanTask(
        id=row["id"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        content=row["content"],
        lane=Lane(row["lane"]),
        agent_class=AgentClass(row["agent_class"]) if row["agent_class"] else None,
        agent_id=row["agent_id"],
        parent_id=row["parent_id"],
        priority=row["priority"],
        created_at=row["created_at"],
        assigned_at=row["assigned_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        result=row["result"],
        error=row["error"],
        message_id=row["message_id"],
    )
