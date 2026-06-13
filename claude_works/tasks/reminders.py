"""User reminder system.

Reminders are stored in the `reminders` table and fired by a background
watcher task. No LLM involved — the stored message is sent directly via
Telegram API when the due time is reached.

Datetime parsing supports:
  - '+Xm', '+Xh', '+Xd'                 →  relative (minutes/hours/days from now)
  - 'HH:MM'                              →  today at that time (next occurrence)
  - 'DD.MM. HH:MM'                       →  German short date (current year)
  - 'DD.MM.YYYY HH:MM'                   →  German full date+time
  - 'DD.MM.YYYY'                         →  German date at midnight
  - 'YYYY-MM-DD HH:MM'                   →  ISO 8601 absolute
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger(__name__)

_RELATIVE_RE = re.compile(r'^\+(\d+)([mhd])$', re.IGNORECASE)
_TIME_ONLY_RE = re.compile(r'^(\d{1,2}):(\d{2})$')
# Matches DD.MM. (with trailing dot, no year)
_DE_SHORT_DATE_RE = re.compile(r'^(\d{1,2})\.(\d{1,2})\.\s+(\d{1,2}):(\d{2})$')
# Matches DD.MM.YYYY
_DE_DATE_RE = re.compile(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$')
# Matches DD.MM.YYYY HH:MM
_DE_DATETIME_RE = re.compile(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})$')

# UTC+2 offset for Europe/Berlin (CEST) — approximation; ±1h on DST boundary is acceptable
_BERLIN_OFFSET = timedelta(hours=2)


def _berlin_to_utc_ts(naive_berlin: datetime) -> int:
    """Convert naive Berlin-local datetime to UTC Unix timestamp."""
    return int((naive_berlin - _BERLIN_OFFSET).replace(tzinfo=timezone.utc).timestamp())


def parse_remind_at(dt_str: str) -> int | None:
    """Parse reminder datetime string → Unix timestamp, or None on failure."""
    dt_str = dt_str.strip()
    now = datetime.now(tz=timezone.utc)
    berlin_now = (now + _BERLIN_OFFSET).replace(tzinfo=None)  # naive Berlin time

    # Relative: +30m, +2h, +1d
    m = _RELATIVE_RE.match(dt_str)
    if m:
        value, unit = int(m.group(1)), m.group(2).lower()
        delta = {"m": timedelta(minutes=value), "h": timedelta(hours=value), "d": timedelta(days=value)}[unit]
        return int((now + delta).timestamp())

    # Time only: HH:MM → today (or tomorrow if already past), Berlin local
    m = _TIME_ONLY_RE.match(dt_str)
    if m:
        candidate = berlin_now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if candidate <= berlin_now:
            candidate += timedelta(days=1)
        return _berlin_to_utc_ts(candidate)

    # German short date: "DD.MM. HH:MM" → current year
    m = _DE_SHORT_DATE_RE.match(dt_str)
    if m:
        d, mo, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        try:
            local = datetime(berlin_now.year, mo, d, h, mi)
            if local <= berlin_now:
                local = local.replace(year=berlin_now.year + 1)
            return _berlin_to_utc_ts(local)
        except ValueError:
            return None

    # German date only: "DD.MM.YYYY" → midnight Berlin
    m = _DE_DATE_RE.match(dt_str)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return _berlin_to_utc_ts(datetime(y, mo, d, 0, 0))
        except ValueError:
            return None

    # German full datetime: "DD.MM.YYYY HH:MM"
    m = _DE_DATETIME_RE.match(dt_str)
    if m:
        d, mo, y, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        try:
            return _berlin_to_utc_ts(datetime(y, mo, d, h, mi))
        except ValueError:
            return None

    # ISO-like: 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DDTHH:MM' (treated as Berlin local)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _berlin_to_utc_ts(datetime.strptime(dt_str, fmt))
        except ValueError:
            continue

    # Fallback: dateparser handles arbitrary formats + German natural language
    # ("morgen 15 Uhr", "nächsten Montag", "15 Juni", "in 3 Stunden", ...)
    try:
        import dateparser
        parsed = dateparser.parse(
            dt_str,
            languages=["de", "en"],
            settings={
                "TIMEZONE": "Europe/Berlin",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
                "PREFER_DAY_OF_MONTH": "first",
            },
        )
        if parsed:
            return int(parsed.timestamp())
    except Exception:
        pass

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
