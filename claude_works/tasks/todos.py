"""Simple personal todo list — no LLM involved."""
import time

import aiosqlite


async def add_todo(
    conn: aiosqlite.Connection,
    user_id: int,
    chat_id: int,
    text: str,
    reminder_id: int | None = None,
) -> int:
    now = int(time.time())
    async with conn.execute(
        "INSERT INTO todos (user_id, chat_id, text, done, reminder_id, created_at) VALUES (?, ?, ?, 0, ?, ?)",
        (user_id, chat_id, text, reminder_id, now),
    ) as cur:
        row_id = cur.lastrowid
    await conn.commit()
    return row_id


async def list_todos(conn: aiosqlite.Connection, user_id: int) -> list[dict]:
    async with conn.execute(
        "SELECT id, text, created_at, reminder_id FROM todos WHERE user_id = ? AND done = 0 ORDER BY created_at",
        (user_id,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def done_todo(conn: aiosqlite.Connection, todo_id: int, user_id: int) -> bool:
    now = int(time.time())
    async with conn.execute(
        "UPDATE todos SET done = 1, done_at = ? WHERE id = ? AND user_id = ? AND done = 0",
        (now, todo_id, user_id),
    ) as cur:
        changed = cur.rowcount > 0
    if changed:
        await conn.commit()
    return changed


async def delete_todo(conn: aiosqlite.Connection, todo_id: int, user_id: int) -> bool:
    async with conn.execute(
        "DELETE FROM todos WHERE id = ? AND user_id = ?",
        (todo_id, user_id),
    ) as cur:
        changed = cur.rowcount > 0
    if changed:
        await conn.commit()
    return changed
