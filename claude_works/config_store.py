import json
import time

import aiosqlite


async def save_config(conn: aiosqlite.Connection, cfg: dict) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO daemon_config (id, settings_json, updated_at) VALUES (1, ?, ?)",
        (json.dumps(cfg), int(time.time())),
    )
    await conn.commit()


async def load_config(conn: aiosqlite.Connection) -> dict | None:
    async with conn.execute("SELECT settings_json FROM daemon_config WHERE id = 1") as cur:
        row = await cur.fetchone()
    return json.loads(row[0]) if row else None


async def delete_config(conn: aiosqlite.Connection) -> None:
    await conn.execute("DELETE FROM daemon_config WHERE id = 1")
    await conn.commit()
