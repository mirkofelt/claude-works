"""Trust-Level: can_see, effektive Stufe, Gruppen-Logik, Store-Filter."""
import pytest

from claude_works import db as cw_db
from claude_works.auth import trust
from claude_works.knowledge import store


@pytest.fixture
async def conn(tmp_path):
    c = await cw_db.init(str(tmp_path / "t.db"))
    yield c
    await c.close()


# --- can_see / effective_trust (pure) ---

def test_admin_sees_everything():
    admin = {"role": "admin", "trust_level": 2}
    assert trust.effective_trust(admin) == 0
    assert trust.can_see(admin, {"visibility": 0})
    assert trust.can_see(admin, {"visibility": 3})


def test_contact_sees_contact_and_public_only():
    contact = {"role": "user", "trust_level": 2}
    assert not trust.can_see(contact, {"visibility": 0})
    assert not trust.can_see(contact, {"visibility": 1})
    assert trust.can_see(contact, {"visibility": 2})
    assert trust.can_see(contact, {"visibility": 3})


def test_unknown_sees_public_only():
    assert not trust.can_see(None, {"visibility": 2})
    assert trust.can_see(None, {"visibility": 3})


def test_missing_fields_default_safe():
    # Kein visibility-Feld → privat; kein trust_level → Kontakt (2)
    assert not trust.can_see({"role": "user"}, {})
    assert trust.effective_trust({"role": "user"}) == 2


# --- chat_trust: Gruppen ---

async def _add_user(conn, tid, role="user", level=2):
    await conn.execute(
        "INSERT INTO users (telegram_id, role, trust_level, created_at) VALUES (?, ?, ?, 0)",
        (tid, role, level),
    )
    await conn.commit()


async def _add_msg(conn, chat_id, from_id, mid):
    await conn.execute(
        "INSERT INTO messages (telegram_message_id, chat_id, from_user_id, text, timestamp) VALUES (?, ?, ?, 'x', 0)",
        (mid, chat_id, from_id),
    )
    await conn.commit()


async def test_direct_chat_uses_user_level(conn):
    await _add_user(conn, 100, role="admin")
    assert await trust.chat_trust(conn, 100, 100) == 0


async def test_unknown_user_is_level_3(conn):
    assert await trust.chat_trust(conn, 555, 555) == 3


async def test_group_least_privileged_member_wins(conn):
    await _add_user(conn, 100, role="admin")        # Stufe 0
    await _add_user(conn, 200, role="user", level=2)  # Kontakt
    group = -4242
    await _add_msg(conn, group, 100, 1)
    await _add_msg(conn, group, 200, 2)
    # Admin schreibt in Gruppe mit Kontakt → effektiv Stufe 2
    assert await trust.chat_trust(conn, group, 100) == 2


async def test_group_with_stranger_drops_to_public_only(conn):
    await _add_user(conn, 100, role="admin")
    group = -777
    await _add_msg(conn, group, 100, 1)
    await _add_msg(conn, group, 999, 2)  # Unbekannter (kein users-Eintrag)
    assert await trust.chat_trust(conn, group, 100) == 3


# --- Store-Filter ---

async def test_search_filters_by_trust(conn):
    await store.add(conn, title="Geheim", content="Kontostand der Familie", visibility=0)
    await store.add(conn, title="Termin", content="Kontostand egal, Treffen Dienstag", visibility=2)
    await store.add(conn, title="Fakt", content="Kontostand öffentlich bekannt", visibility=3)

    owner = await store.search(conn, "Kontostand", trust=0)
    contact = await store.search(conn, "Kontostand", trust=2)
    stranger = await store.search(conn, "Kontostand", trust=3)

    assert {e["title"] for e in owner} == {"Geheim", "Termin", "Fakt"}
    assert {e["title"] for e in contact} == {"Termin", "Fakt"}
    assert {e["title"] for e in stranger} == {"Fakt"}


async def test_list_and_count_filter_by_trust(conn):
    await store.add(conn, title="a", content="x", visibility=0)
    await store.add(conn, title="b", content="x", visibility=2)
    assert await store.count(conn, trust=0) == 2
    assert await store.count(conn, trust=2) == 1
    assert {e["title"] for e in await store.list_all(conn, trust=2)} == {"b"}


async def test_default_visibility_is_private(conn):
    eid = await store.add(conn, title="neu", content="agent save")
    entry = await store.get(conn, eid)
    assert entry["visibility"] == 0
