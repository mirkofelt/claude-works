"""User reminder system.

Reminders are stored in the `reminders` table and fired by a background
watcher task. No LLM involved — the stored message is sent directly via
Telegram API when the due time is reached.

Datetime parsing supports:
  - ISO 8601 / 'YYYY-MM-DD HH:MM'       →  absolute
  - 'HH:MM'                              →  today at that time (next occurrence)
  - '+Xm', '+Xh', '+Xd'                 →  relative (minutes/hours/days from now)
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger(__name__)

_RELATIVE_RE = re.compile(r'^\+(\d+)([mhd])$', re.IGNORECASE)
_TIME_ONLY_RE = re.compile(r'^(\d{1,2}):(\d{2})$')


def parse_remind_at(dt_str: str) -> int | None:
    """Parse reminder datetime string → Unix timestamp, or None on failure."""
    dt_str = dt_str.strip()
    now = datetime.now(tz=timezone.utc)

    # Relative: +30m, +2h, +1d
    m = _RELATIVE_RE.match(dt_str)
    if m:
        value, unit = int(m.group(1)), m.group(2).lower()
        delta = {"m": timedelta(minutes=value), "h": timedelta(hours=value), "d": timedelta(days=value)}[unit]
        return int((now + delta).timestamp())

    # Time only: HH:MM → today (or tomorrow if already past)
    m = _TIME_ONLY_RE.match(dt_str)
    if m:
        candidate = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return int(candidate.timestamp())

    # ISO-like: 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DDTHH:MM'
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(dt_str, fmt)
            # Treat as local time (Europe/Berlin) → simplistic UTC offset not critical for reminders
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue

    return None


async def add_reminder(
    conn: aiosqlite.Connection,
    user_id: int,
    chat_id: int,
    remind_at: int,
    message: str,
) -> int:
    """Insert a reminder row. Returns the new reminder ID."""
    now = int(time.time())
    async with conn.execute(
        "INSERT INTO reminders (user_id, chat_id, remind_at, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, chat_id, remind_at, message, now),
    ) as cur:
        row_id = cur.lastrowid
    await conn.commit()
    return row_id


async def list_reminders(conn: aiosqlite.Connection, user_id: int) -> list[dict]:
    """Return pending reminders for a user."""
    async with conn.execute(
        "SELECT id, chat_id, remind_at, message FROM reminders WHERE user_id = ? AND fired_at IS NULL ORDER BY remind_at ASC",
        (user_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete_reminder(conn: aiosqlite.Connection, reminder_id: int, user_id: int) -> bool:
    """Delete a reminder. Returns True if deleted."""
    async with conn.execute(
        "DELETE FROM reminders WHERE id = ? AND user_id = ? AND fired_at IS NULL",
        (reminder_id, user_id),
    ) as cur:
        deleted = cur.rowcount > 0
    await conn.commit()
    return deleted


async def fire_due_reminders(conn: aiosqlite.Connection) -> list[dict]:
    """Fetch all due unfired reminders and mark them fired. Returns list of fired reminders."""
    now = int(time.time())
    async with conn.execute(
        "SELECT id, user_id, chat_id, remind_at, message FROM reminders WHERE fired_at IS NULL AND remind_at <= ?",
        (now,),
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        return []

    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    await conn.execute(
        f"UPDATE reminders SET fired_at = ? WHERE id IN ({placeholders})",
        [now, *ids],
    )
    await conn.commit()
    return [dict(r) for r in rows]
