import json
import logging
import os
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_KNOWLEDGE_DIR = Path(os.environ.get("KNOWLEDGE_DIR", "/data/knowledge"))
_IMPORT_EXTENSIONS = {".md", ".txt"}


async def import_from_directory(conn: aiosqlite.Connection, directory: Path | None = None) -> int:
    """Scan directory for .md/.txt files and import new/updated entries into KB.
    Uses source=file::<name> as dedup key. Re-imports if file mtime > last import time.
    Returns count of files imported or updated."""
    d = directory or _KNOWLEDGE_DIR
    if not d.exists():
        return 0

    imported = 0
    for path in sorted(d.iterdir()):
        if path.suffix not in _IMPORT_EXTENSIONS or path.name.startswith('.'):
            continue
        try:
            file_mtime = int(path.stat().st_mtime)
            source_key = f"file::{path.name}"

            async with conn.execute(
                "SELECT id, updated_at FROM knowledge WHERE source = ? LIMIT 1",
                (source_key,),
            ) as cur:
                row = await cur.fetchone()

            if row and row["updated_at"] >= file_mtime:
                continue  # already up to date

            content = path.read_text(encoding="utf-8").strip()
            title = path.stem.replace("_", " ").replace("-", " ").title()

            if row:
                await update(conn, row["id"], content=content)
                logger.info("knowledge re-imported (changed): %s", path.name)
            else:
                await add(conn, title=title, content=content, type="document", source=source_key)
                logger.info("knowledge imported: %s", path.name)
            imported += 1
        except Exception as e:
            logger.warning("knowledge import failed for %s: %s", path.name, e)

    return imported


async def add(
    conn: aiosqlite.Connection,
    *,
    title: str,
    content: str,
    type: str = "note",
    tags: list[str] | None = None,
    source: str = "agent",
    user_id: int | None = None,
    visibility: int = 0,
) -> int:
    now = int(time.time())
    tags_json = json.dumps(tags or [])
    async with conn.execute(
        """INSERT INTO knowledge (type, title, content, tags, source, user_id, visibility, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (type, title, content, tags_json, source, user_id, visibility, now, now),
    ) as cur:
        entry_id = cur.lastrowid
    await conn.commit()
    logger.info("knowledge add id=%d title=%r type=%s source=%s", entry_id, title, type, source)
    return entry_id  # type: ignore[return-value]


async def update(
    conn: aiosqlite.Connection,
    entry_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    type: str | None = None,
    tags: list[str] | None = None,
    visibility: int | None = None,
) -> bool:
    now = int(time.time())
    sets: list[str] = ["updated_at = ?"]
    params: list = [now]
    if visibility is not None:
        sets.append("visibility = ?")
        params.append(visibility)
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if content is not None:
        sets.append("content = ?")
        params.append(content)
    if type is not None:
        sets.append("type = ?")
        params.append(type)
    if tags is not None:
        sets.append("tags = ?")
        params.append(json.dumps(tags))
    params.append(entry_id)
    async with conn.execute(
        f"UPDATE knowledge SET {', '.join(sets)} WHERE id = ?", params
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


_FTS_MIN_SCORE = -0.1  # BM25 scores are negative; filter near-zero relevance hits


async def search(conn: aiosqlite.Connection, query: str, user_id: int | None = None, limit: int = 10, trust: int = 0) -> list[dict]:
    """Full-text search via FTS5 with LIKE fallback for short or special queries.

    trust: effektive Vertrauensstufe des Lesers — nur Einträge mit
    visibility >= trust werden geliefert (0 = Owner, sieht alles)."""
    if not query or not query.strip():
        return []

    extra_sql = ""
    extra_params: list = []
    if user_id is not None:
        extra_sql += " AND (k.user_id = ? OR k.user_id IS NULL)"
        extra_params.append(user_id)
    if trust > 0:
        extra_sql += " AND k.visibility >= ?"
        extra_params.append(trust)

    # Try FTS5 first (better recall, BM25 ranking)
    try:
        fts_query = " OR ".join(f'"{w}"' for w in query.split() if w)
        async with conn.execute(
            f"""SELECT k.* FROM knowledge k
               JOIN knowledge_fts ON knowledge_fts.rowid = k.id
               WHERE knowledge_fts MATCH ?{extra_sql}
                 AND rank < ?
               ORDER BY rank LIMIT ?""",
            (fts_query, *extra_params, _FTS_MIN_SCORE, limit),
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            return [_row_to_dict(r) for r in rows]
    except Exception:
        pass  # fall through to LIKE

    # LIKE fallback
    pattern = f"%{query}%"
    async with conn.execute(
        f"""SELECT * FROM knowledge k
           WHERE (k.title LIKE ? OR k.content LIKE ? OR k.tags LIKE ?){extra_sql}
           ORDER BY k.updated_at DESC LIMIT ?""",
        (pattern, pattern, pattern, *extra_params, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def count(conn: aiosqlite.Connection, user_id: int | None = None, type: str | None = None, trust: int = 0) -> int:
    conditions: list[str] = []
    params: list = []
    if user_id is not None:
        conditions.append("(user_id = ? OR user_id IS NULL)")
        params.append(user_id)
    if type is not None:
        conditions.append("type = ?")
        params.append(type)
    if trust > 0:
        conditions.append("visibility >= ?")
        params.append(trust)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    async with conn.execute(f"SELECT COUNT(*) FROM knowledge {where}", params) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def list_all(conn: aiosqlite.Connection, user_id: int | None = None, type: str | None = None, limit: int = 25, offset: int = 0, trust: int = 0) -> list[dict]:
    conditions: list[str] = []
    params: list = []
    if user_id is not None:
        conditions.append("(user_id = ? OR user_id IS NULL)")
        params.append(user_id)
    if type is not None:
        conditions.append("type = ?")
        params.append(type)
    if trust > 0:
        conditions.append("visibility >= ?")
        params.append(trust)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.extend([limit, offset])
    async with conn.execute(
        f"SELECT * FROM knowledge {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?", params
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
