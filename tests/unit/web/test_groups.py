"""Tests for /api/groups CRUD endpoints."""
import pytest
import aiosqlite
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient, ASGITransport

from claude_works.db import CREATE_TABLES, CONFIG_TABLES
from claude_works import config as cfg_mod
from claude_works.web.app import app, _verify_token


async def _make_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CREATE_TABLES)
    await conn.executescript(CONFIG_TABLES)
    await conn.commit()
    return conn


@pytest.fixture
def client():
    app.dependency_overrides[_verify_token] = lambda: None
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.pop(_verify_token, None)


@pytest.fixture(autouse=True)
def base_config():
    cfg_mod.set({"groups": {}})
    yield


async def _patched_post(client, body):
    conn = await _make_conn()
    with patch("claude_works.web.app.db.init_config", AsyncMock(return_value=conn)):
        async with client as c:
            r = await c.post("/api/groups", json=body)
    await conn.close()
    return r


@pytest.mark.asyncio
async def test_list_groups_empty(client):
    async with client as c:
        r = await c.get("/api/groups")
    assert r.status_code == 200
    assert r.json() == {"groups": {}}


@pytest.mark.asyncio
async def test_upsert_group_happy(client):
    body = {
        "chat_id": -1001234567890,
        "persona": "Du bist der Hausmeister.",
        "focus": "Nur Hausautomation",
        "communication_style": "Knapp, Deutsch",
    }
    r = await _patched_post(client, body)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["chat_id"] == "-1001234567890"
    groups = cfg_mod.section("groups")
    assert groups["-1001234567890"]["persona"] == "Du bist der Hausmeister."
    assert groups["-1001234567890"]["focus"] == "Nur Hausautomation"


@pytest.mark.asyncio
async def test_upsert_drops_empty_fields(client):
    body = {"chat_id": -100, "persona": "  ", "focus": "x", "communication_style": ""}
    r = await _patched_post(client, body)
    assert r.status_code == 200
    entry = cfg_mod.section("groups")["-100"]
    assert entry == {"focus": "x"}


@pytest.mark.asyncio
async def test_upsert_rejects_positive_id(client):
    r = await _patched_post(client, {"chat_id": 123, "persona": "x"})
    assert r.status_code == 400
    assert "negative" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upsert_rejects_non_integer(client):
    r = await _patched_post(client, {"chat_id": "abc"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_group(client):
    cfg_mod.set({"groups": {"-100": {"focus": "x"}}})
    conn = await _make_conn()
    with patch("claude_works.web.app.db.init_config", AsyncMock(return_value=conn)):
        async with client as c:
            r = await c.delete("/api/groups/-100")
    await conn.close()
    assert r.status_code == 200
    assert cfg_mod.section("groups") == {}


@pytest.mark.asyncio
async def test_delete_missing_group_404(client):
    async with client as c:
        r = await c.delete("/api/groups/-999")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_removes_legacy_int_key(client):
    cfg_mod.set({"groups": {-100: {"focus": "x"}}})
    conn = await _make_conn()
    with patch("claude_works.web.app.db.init_config", AsyncMock(return_value=conn)):
        async with client as c:
            r = await c.delete("/api/groups/-100")
    await conn.close()
    assert r.status_code == 200
    assert cfg_mod.section("groups") == {}
