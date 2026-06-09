import json
import time

import aiosqlite


async def set(conn: aiosqlite.Connection, key: str, value: str, user_id: int | None = None, tags: list[str] | None = None) -> None:
    now = int(time.time())
    tags_str = json.dumps(tags) if tags else None
    await conn.execute(
        """INSERT INTO memory (user_id, key, value, tags, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, tags=excluded.tags, updated_at=excluded.updated_at""",
        (user_id, key, value, tags_str, now, now),
    )
    await conn.commit()


async def get(conn: aiosqlite.Connection, key: str, user_id: int | None = None) -> str | None:
    async with conn.execute(
        "SELECT value FROM memory WHERE key = ? AND (user_id = ? OR user_id IS NULL) ORDER BY user_id DESC LIMIT 1",
        (key, user_id),
    ) as cur:
        row = await cur.fetchone()
    return row["value"] if row else None


async def search(conn: aiosqlite.Connection, query: str, user_id: int | None = None, limit: int = 20) -> list[dict]:
    async with conn.execute(
        """SELECT * FROM memory WHERE (key LIKE ? OR value LIKE ?)
           AND (user_id = ? OR user_id IS NULL)
           ORDER BY updated_at DESC LIMIT ?""",
        (f"%{query}%", f"%{query}%", user_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete(conn: aiosqlite.Connection, key: str, user_id: int | None = None) -> None:
    await conn.execute("DELETE FROM memory WHERE key = ? AND user_id IS ?", (key, user_id))
    await conn.commit()


async def list_all(conn: aiosqlite.Connection, user_id: int | None = None, limit: int = 100) -> list[dict]:
    async with conn.execute(
        "SELECT * FROM memory WHERE user_id IS ? ORDER BY updated_at DESC LIMIT ?",
        (user_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]
