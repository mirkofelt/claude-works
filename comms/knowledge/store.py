import json
import logging
import time

import aiosqlite

logger = logging.getLogger(__name__)


class KnowledgeStore:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def add(
        self,
        *,
        title: str,
        content: str,
        type: str = "note",
        tags: list[str] | None = None,
        source: str = "system",
        user_id: int | None = None,
    ) -> int:
        now = int(time.time())
        tags_json = json.dumps(tags or [])
        async with self._conn.execute(
            """INSERT INTO knowledge (type, title, content, tags, source, user_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (type, title, content, tags_json, source, user_id, now, now),
        ) as cur:
            entry_id = cur.lastrowid
        await self._conn.commit()
        logger.info("Knowledge add id=%d title=%r type=%s", entry_id, title, type)
        return entry_id  # type: ignore[return-value]

    async def update(self, entry_id: int, *, content: str, tags: list[str] | None = None) -> bool:
        now = int(time.time())
        tags_json = json.dumps(tags) if tags is not None else None
        if tags_json is not None:
            async with self._conn.execute(
                "UPDATE knowledge SET content = ?, tags = ?, updated_at = ? WHERE id = ?",
                (content, tags_json, now, entry_id),
            ) as cur:
                updated = cur.rowcount
        else:
            async with self._conn.execute(
                "UPDATE knowledge SET content = ?, updated_at = ? WHERE id = ?",
                (content, now, entry_id),
            ) as cur:
                updated = cur.rowcount
        await self._conn.commit()
        return updated > 0

    async def delete(self, entry_id: int) -> bool:
        async with self._conn.execute(
            "DELETE FROM knowledge WHERE id = ?", (entry_id,)
        ) as cur:
            deleted = cur.rowcount
        await self._conn.commit()
        return deleted > 0

    async def get(self, entry_id: int) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM knowledge WHERE id = ?", (entry_id,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_dict(row) if row else None

    async def search(self, query: str, user_id: int | None = None, limit: int = 20) -> list[dict]:
        pattern = f"%{query}%"
        if user_id is not None:
            async with self._conn.execute(
                """SELECT * FROM knowledge
                   WHERE (user_id = ? OR user_id IS NULL)
                     AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)
                   ORDER BY updated_at DESC LIMIT ?""",
                (user_id, pattern, pattern, pattern, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with self._conn.execute(
                """SELECT * FROM knowledge
                   WHERE title LIKE ? OR content LIKE ? OR tags LIKE ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (pattern, pattern, pattern, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def list_all(self, user_id: int | None = None, type: str | None = None, limit: int = 100) -> list[dict]:
        conditions = []
        params: list = []
        if user_id is not None:
            conditions.append("(user_id = ? OR user_id IS NULL)")
            params.append(user_id)
        if type is not None:
            conditions.append("type = ?")
            params.append(type)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        async with self._conn.execute(
            f"SELECT * FROM knowledge {where} ORDER BY updated_at DESC LIMIT ?", params
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: aiosqlite.Row) -> dict:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    return d
