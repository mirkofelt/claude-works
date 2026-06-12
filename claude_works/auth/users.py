import time
import logging

import aiosqlite

from ..config import section

logger = logging.getLogger(__name__)

ROLES = ("admin", "user", "blocked")


async def get_user(conn: aiosqlite.Connection, telegram_id: int) -> dict | None:
    async with conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_user(conn: aiosqlite.Connection, telegram_id: int, name: str | None = None) -> dict:
    cfg = section("users")
    admin_ids: list[int] = cfg.get("admin_ids", [])
    default_role = "admin" if telegram_id in admin_ids else cfg.get("default_role", "blocked")

    now = int(time.time())
    existing = await get_user(conn, telegram_id)
    if existing:
        await conn.execute(
            "UPDATE users SET last_seen = ?, name = COALESCE(?, name) WHERE telegram_id = ?",
            (now, name, telegram_id),
        )
        await conn.commit()
        return await get_user(conn, telegram_id)  # type: ignore[return-value]

    await conn.execute(
        "INSERT INTO users (telegram_id, name, role, created_at, last_seen) VALUES (?, ?, ?, ?, ?)",
        (telegram_id, name, default_role, now, now),
    )
    await conn.commit()
    user = await get_user(conn, telegram_id)
    if default_role == "blocked":
        logger.info("New user %s (%s) blocked — awaiting admin approval", telegram_id, name)
    return user  # type: ignore[return-value]


async def set_role(conn: aiosqlite.Connection, telegram_id: int, role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role}")
    await conn.execute("UPDATE users SET role = ? WHERE telegram_id = ?", (role, telegram_id))
    await conn.commit()
    logger.info("User %d role set to %s", telegram_id, role)


async def set_trust(conn: aiosqlite.Connection, telegram_id: int, level: int) -> bool:
    """Set trust_level (0=Owner, 1=Vertraut, 2=Kontakt, 3=Unbekannt). Returns False if user unknown."""
    if level not in (0, 1, 2, 3):
        raise ValueError(f"Invalid trust level: {level}")
    async with conn.execute(
        "UPDATE users SET trust_level = ? WHERE telegram_id = ?", (level, telegram_id)
    ) as cur:
        updated = cur.rowcount
    await conn.commit()
    if updated:
        logger.info("User %d trust_level set to %d", telegram_id, level)
    return updated > 0


async def is_allowed(conn: aiosqlite.Connection, telegram_id: int) -> bool:
    user = await get_user(conn, telegram_id)
    allowed = user is not None and user["role"] in ("admin", "user")
    if not allowed:
        role = user["role"] if user else "unknown"
        logger.warning("Access denied for user %d (role=%s)", telegram_id, role)
    return allowed


async def is_admin(conn: aiosqlite.Connection, telegram_id: int) -> bool:
    user = await get_user(conn, telegram_id)
    return user is not None and user["role"] == "admin"
