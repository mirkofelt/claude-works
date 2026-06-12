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


@pytest.mark.asyncio
async def test_upsert_with_echo_filter(client):
    """Test that echo_filter boolean field is saved correctly."""
    body = {
        "chat_id": -100,
        "persona": "test",
        "echo_filter": True,
    }
    r = await _patched_post(client, body)
    assert r.status_code == 200
    entry = cfg_mod.section("groups")["-100"]
    assert entry["echo_filter"] is True
    assert entry["persona"] == "test"


@pytest.mark.asyncio
async def test_upsert_with_echo_filter_false(client):
    """Test that echo_filter false is not stored (treat as None)."""
    body = {
        "chat_id": -100,
        "persona": "test",
        "echo_filter": False,
    }
    r = await _patched_post(client, body)
    assert r.status_code == 200
    entry = cfg_mod.section("groups")["-100"]
    assert "echo_filter" not in entry


@pytest.mark.asyncio
async def test_upsert_with_truncation_limit(client):
    """Test that truncation_limit numeric field is saved correctly."""
    body = {
        "chat_id": -100,
        "persona": "test",
        "truncation_limit": 2000,
    }
    r = await _patched_post(client, body)
    assert r.status_code == 200
    entry = cfg_mod.section("groups")["-100"]
    assert entry["truncation_limit"] == 2000


@pytest.mark.asyncio
async def test_upsert_with_truncation_limit_zero(client):
    """Test that truncation_limit of 0 is not stored (disabled)."""
    body = {
        "chat_id": -100,
        "persona": "test",
        "truncation_limit": 0,
    }
    r = await _patched_post(client, body)
    assert r.status_code == 200
    entry = cfg_mod.section("groups")["-100"]
    assert "truncation_limit" not in entry


@pytest.mark.asyncio
async def test_upsert_rejects_negative_truncation_limit(client):
    """Test that negative truncation_limit is rejected."""
    body = {
        "chat_id": -100,
        "truncation_limit": -100,
    }
    r = await _patched_post(client, body)
    assert r.status_code == 400
    assert "non-negative" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upsert_with_model_override(client):
    """Test that model_override is saved correctly."""
    body = {
        "chat_id": -100,
        "persona": "test",
        "model_override": "claude-opus-4-8",
    }
    r = await _patched_post(client, body)
    assert r.status_code == 200
    entry = cfg_mod.section("groups")["-100"]
    assert entry["model_override"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_upsert_all_settings_together(client):
    """Test that all settings can be saved together."""
    body = {
        "chat_id": -1001234567890,
        "persona": "Assistant",
        "focus": "Tech support",
        "communication_style": "Professional",
        "echo_filter": True,
        "truncation_limit": 3000,
        "model_override": "claude-sonnet-4-6",
    }
    r = await _patched_post(client, body)
    assert r.status_code == 200
    entry = cfg_mod.section("groups")["-1001234567890"]
    assert entry["persona"] == "Assistant"
    assert entry["focus"] == "Tech support"
    assert entry["communication_style"] == "Professional"
    assert entry["echo_filter"] is True
    assert entry["truncation_limit"] == 3000
    assert entry["model_override"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_upsert_echo_filter_string_true(client):
    """Test that echo_filter accepts string 'true'."""
    body = {
        "chat_id": -100,
        "echo_filter": "true",
    }
    r = await _patched_post(client, body)
    assert r.status_code == 200
    entry = cfg_mod.section("groups")["-100"]
    assert entry["echo_filter"] is True


@pytest.mark.asyncio
async def test_upsert_truncation_limit_string_number(client):
    """Test that truncation_limit accepts string numbers."""
    body = {
        "chat_id": -100,
        "truncation_limit": "2500",
    }
    r = await _patched_post(client, body)
    assert r.status_code == 200
    entry = cfg_mod.section("groups")["-100"]
    assert entry["truncation_limit"] == 2500
