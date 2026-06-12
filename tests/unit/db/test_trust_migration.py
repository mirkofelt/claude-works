"""Trust-level migration: rückwärtskompatibel für alte DBs ohne trust_level/visibility."""
import aiosqlite
import pytest

from claude_works import db as cw_db

# Altes Schema (vor Trust-Levels) — Minimalversion von users + knowledge
_OLD_SCHEMA = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    telegram_id INTEGER UNIQUE NOT NULL,
    name TEXT,
    role TEXT NOT NULL DEFAULT 'blocked',
    created_at INTEGER NOT NULL,
    last_seen INTEGER
);
CREATE TABLE knowledge (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'note',
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT,
    source TEXT,
    user_id INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


async def _columns(conn, table):
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        rows = await cur.fetchall()
    return {r["name"]: r for r in rows}


async def test_fresh_db_has_trust_columns(tmp_path):
    conn = await cw_db.init(str(tmp_path / "fresh.db"))
    try:
        users_cols = await _columns(conn, "users")
        kb_cols = await _columns(conn, "knowledge")
        assert "trust_level" in users_cols
        assert "visibility" in kb_cols
        assert users_cols["trust_level"]["dflt_value"] == "2"
        assert kb_cols["visibility"]["dflt_value"] == "0"
    finally:
        await conn.close()


async def test_old_db_migrates_with_backfill(tmp_path):
    path = str(tmp_path / "old.db")
    # Alte DB mit Bestandsdaten anlegen
    conn = await aiosqlite.connect(path)
    await conn.executescript(_OLD_SCHEMA)
    await conn.execute(
        "INSERT INTO users (telegram_id, name, role, created_at) VALUES (1, 'tobi', 'admin', 0)"
    )
    await conn.execute(
        "INSERT INTO users (telegram_id, name, role, created_at) VALUES (2, 'stefan', 'user', 0)"
    )
    await conn.execute(
        "INSERT INTO knowledge (title, content, created_at, updated_at) VALUES ('t', 'c', 0, 0)"
    )
    await conn.commit()
    await conn.close()

    # Migration via init()
    conn = await cw_db.init(path)
    try:
        async with conn.execute("SELECT trust_level FROM users WHERE telegram_id = 1") as cur:
            row = await cur.fetchone()
        assert row["trust_level"] == 0  # Admin-Backfill → Owner-Stufe
        async with conn.execute("SELECT trust_level FROM users WHERE telegram_id = 2") as cur:
            row = await cur.fetchone()
        assert row["trust_level"] == 2  # Default für Bestandsnutzer

        async with conn.execute("SELECT visibility FROM knowledge") as cur:
            rows = await cur.fetchall()
        assert rows and all(r["visibility"] == 0 for r in rows)  # alles privat
    finally:
        await conn.close()


async def test_migration_idempotent(tmp_path):
    path = str(tmp_path / "twice.db")
    conn = await cw_db.init(path)
    await conn.close()
    conn = await cw_db.init(path)  # zweiter Lauf darf nicht crashen
    try:
        assert "trust_level" in await _columns(conn, "users")
    finally:
        await conn.close()
