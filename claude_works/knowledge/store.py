import json
import logging
import time

import aiosqlite

logger = logging.getLogger(__name__)


async def add(
    conn: aiosqlite.Connection,
    *,
    title: str,
    content: str,
    type: str = "note",
    tags: list[str] | None = None,
    source: str = "agent",
    user_id: int | None = None,
) -> int:
    now = int(time.time())
    tags_json = json.dumps(tags or [])
    async with conn.execute(
        """INSERT INTO knowledge (type, title, content, tags, source, user_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (type, title, content, tags_json, source, user_id, now, now),
    ) as cur:
        entry_id = cur.lastrowid
    await conn.commit()
    logger.info("knowledge add id=%d title=%r type=%s source=%s", entry_id, title, type, source)
    return entry_id  # type: ignore[return-value]


async def update(conn: aiosqlite.Connection, entry_id: int, *, content: str, tags: list[str] | None = None) -> bool:
    now = int(time.time())
    tags_json = json.dumps(tags) if tags is not None else None
    if tags_json is not None:
        async with conn.execute(
            "UPDATE knowledge SET content = ?, tags = ?, updated_at = ? WHERE id = ?",
            (content, tags_json, now, entry_id),
        ) as cur:
            updated = cur.rowcount
    else:
        async with conn.execute(
            "UPDATE knowledge SET content = ?, updated_at = ? WHERE id = ?",
            (content, now, entry_id),
        ) as cur:
            updated = cur.rowcount
    await conn.commit()
    return updated > 0


async def delete(conn: aiosqlite.Connection, entry_id: int) -> bool:
    async with conn.execute("DELETE FROM knowledge WHERE id = ?", (entry_id,)) as cur:
        deleted = cur.rowcount
    await conn.commit()
    return deleted > 0


async def get(conn: aiosqlite.Connection, entry_id: int) -> dict | None:
    async with conn.execute("SELECT * FROM knowledge WHERE id = ?", (entry_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def search(conn: aiosqlite.Connection, query: str, user_id: int | None = None, limit: int = 10) -> list[dict]:
    """Full-text search via FTS5 with LIKE fallback for short or special queries."""
    if not query or not query.strip():
        return await list_all(conn, user_id=user_id, limit=limit)

    # Try FTS5 first (better recall, BM25 ranking)
    try:
        fts_query = " OR ".join(f'"{w}"' for w in query.split() if w)
        if user_id is not None:
            async with conn.execute(
                """SELECT k.* FROM knowledge k
                   JOIN knowledge_fts ON knowledge_fts.rowid = k.id
                   WHERE knowledge_fts MATCH ?
                     AND (k.user_id = ? OR k.user_id IS NULL)
                   ORDER BY rank LIMIT ?""",
                (fts_query, user_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            async with conn.execute(
                """SELECT k.* FROM knowledge k
                   JOIN knowledge_fts ON knowledge_fts.rowid = k.id
                   WHERE knowledge_fts MATCH ?
                   ORDER BY rank LIMIT ?""",
                (fts_query, limit),
            ) as cur:
                rows = await cur.fetchall()
        if rows:
            return [_row_to_dict(r) for r in rows]
    except Exception:
        pass  # fall through to LIKE

    # LIKE fallback
    pattern = f"%{query}%"
    if user_id is not None:
        async with conn.execute(
            """SELECT * FROM knowledge
               WHERE (user_id = ? OR user_id IS NULL)
                 AND (title LIKE ? OR content LIKE ? OR tags LIKE ?)
               ORDER BY updated_at DESC LIMIT ?""",
            (user_id, pattern, pattern, pattern, limit),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with conn.execute(
            """SELECT * FROM knowledge
               WHERE title LIKE ? OR content LIKE ? OR tags LIKE ?
               ORDER BY updated_at DESC LIMIT ?""",
            (pattern, pattern, pattern, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def list_all(conn: aiosqlite.Connection, user_id: int | None = None, type: str | None = None, limit: int = 100) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    if user_id is not None:
        conditions.append("(user_id = ? OR user_id IS NULL)")
        params.append(user_id)
    if type is not None:
        conditions.append("type = ?")
        params.append(type)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    async with conn.execute(
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
