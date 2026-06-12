"""Trust levels & KB visibility.

Stufen (users.trust_level):
    0 = Owner/Admin    — sieht alles (role='admin' wird immer auf 0 gemappt)
    2 = Kontakt        — Default für freigeschaltete Nutzer
    3 = Unbekannt      — sieht nur Öffentliches

Schutzstufen (knowledge.visibility):
    0 = privat/geheim  — nur Owner/Admin
    2 = Kontakte       — vorgestellte Kontakte + Admin
    3 = öffentlich     — jeder

Regel: Eintrag sichtbar wenn visibility >= trust_level.

Gruppen: effektive Stufe = lockerste Vertrauensstufe aller Chat-Mitglieder,
d.h. das am wenigsten vertraute Mitglied bestimmt (max()). Ein Unbekannter
in der Gruppe → nur öffentliche Einträge für den ganzen Chat.
"""
import logging

import aiosqlite

logger = logging.getLogger(__name__)

TRUST_OWNER = 0
TRUST_CONTACT = 2
TRUST_UNKNOWN = 3

VISIBILITY_PRIVATE = 0
VISIBILITY_CONTACTS = 2
VISIBILITY_PUBLIC = 3

TRUST_LEVELS = (TRUST_OWNER, 1, TRUST_CONTACT, TRUST_UNKNOWN)
VISIBILITY_LEVELS = (VISIBILITY_PRIVATE, 1, VISIBILITY_CONTACTS, VISIBILITY_PUBLIC)

TRUST_LABELS = {0: "Owner", 1: "Vertraut", 2: "Kontakt", 3: "Unbekannt"}
VISIBILITY_LABELS = {0: "privat", 1: "vertraut", 2: "Kontakte", 3: "öffentlich"}


def effective_trust(user: dict | None) -> int:
    """Effektive Vertrauensstufe eines Users. Admins sind immer Stufe 0."""
    if user is None:
        return TRUST_UNKNOWN
    if user.get("role") == "admin":
        return TRUST_OWNER
    level = user.get("trust_level")
    return level if isinstance(level, int) else TRUST_CONTACT


def can_see(user: dict | None, entry: dict | None) -> bool:
    """Sichtbar wenn kb.visibility >= effektive Vertrauensstufe des Users."""
    if entry is None:
        return False
    visibility = entry.get("visibility")
    if not isinstance(visibility, int):
        visibility = VISIBILITY_PRIVATE
    return visibility >= effective_trust(user)


async def user_trust(conn: aiosqlite.Connection, telegram_id: int | None) -> int:
    """Effektive Stufe eines einzelnen Telegram-Users (DB-Lookup)."""
    if telegram_id is None:
        return TRUST_UNKNOWN
    async with conn.execute(
        "SELECT role, trust_level FROM users WHERE telegram_id = ?", (telegram_id,)
    ) as cur:
        row = await cur.fetchone()
    return effective_trust(dict(row) if row else None)


async def chat_trust(conn: aiosqlite.Connection, chat_id: int | None, user_id: int | None) -> int:
    """Effektive Stufe für einen Chat.

    Direkt-Chat (chat_id >= 0 oder None): Stufe des Users.
    Gruppe (chat_id < 0): lockerste Stufe aller Mitglieder, die je geschrieben
    haben — max() über alle bekannten Absender, mindestens die Stufe des
    aktuellen Absenders.
    """
    base = await user_trust(conn, user_id)
    if chat_id is None or chat_id >= 0:
        return base

    async with conn.execute(
        "SELECT DISTINCT from_user_id FROM messages WHERE chat_id = ? AND from_user_id IS NOT NULL",
        (chat_id,),
    ) as cur:
        rows = await cur.fetchall()

    level = base
    for row in rows:
        member_id = row["from_user_id"]
        if member_id == user_id:
            continue
        level = max(level, await user_trust(conn, member_id))
    if level != base:
        logger.debug("chat %d: group trust raised %d → %d (least-privileged member wins)", chat_id, base, level)
    return level
