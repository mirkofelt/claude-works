"""Tests for /api/setup GET and /api/setup/save POST endpoints."""
import pytest
import aiosqlite
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient, ASGITransport

from claude_works.db import CREATE_TABLES
from claude_works.mode import DaemonMode
from claude_works.web.app import app


def _make_daemon(mode: DaemonMode) -> MagicMock:
    mgr = MagicMock()
    mgr.mode = mode
    daemon = MagicMock()
    daemon._mode_mgr = mgr
    return daemon


async def _make_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CREATE_TABLES)
    await conn.commit()
    return conn


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# GET /api/setup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_setup_no_daemon(client):
    with patch("claude_works.web.app._daemon_ref", None):
        async with client as c:
            r = await c.get("/api/setup")
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "startup"
    assert data["setup_required"] is False


@pytest.mark.asyncio
async def test_get_setup_initialize_mode(client):
    daemon = _make_daemon(DaemonMode.INITIALIZE)
    with patch("claude_works.web.app._daemon_ref", daemon):
        async with client as c:
            r = await c.get("/api/setup")
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "initialize"
    assert data["setup_required"] is True


@pytest.mark.asyncio
async def test_get_setup_run_mode(client):
    daemon = _make_daemon(DaemonMode.RUN)
    with patch("claude_works.web.app._daemon_ref", daemon):
        async with client as c:
            r = await c.get("/api/setup")
    assert r.status_code == 200
    assert r.json()["setup_required"] is False


# ---------------------------------------------------------------------------
# POST /api/setup/save — mode check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_setup_409_when_no_daemon(client):
    with patch("claude_works.web.app._daemon_ref", None):
        async with client as c:
            r = await c.post("/api/setup/save", json={})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_save_setup_409_when_not_initialize(client):
    daemon = _make_daemon(DaemonMode.RUN)
    with patch("claude_works.web.app._daemon_ref", daemon), \
         patch("claude_works.web.app._setup_token", "tok"):
        async with client as c:
            r = await c.post("/api/setup/save", json={}, headers={"X-Setup-Token": "tok"})
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# POST /api/setup/save — token auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_setup_403_no_token(client):
    daemon = _make_daemon(DaemonMode.INITIALIZE)
    with patch("claude_works.web.app._daemon_ref", daemon), \
         patch("claude_works.web.app._setup_token", "secret"):
        async with client as c:
            r = await c.post("/api/setup/save", json={})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_save_setup_403_wrong_token(client):
    daemon = _make_daemon(DaemonMode.INITIALIZE)
    with patch("claude_works.web.app._daemon_ref", daemon), \
         patch("claude_works.web.app._setup_token", "secret"):
        async with client as c:
            r = await c.post("/api/setup/save", json={}, headers={"X-Setup-Token": "wrong"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_save_setup_403_when_token_already_used(client):
    daemon = _make_daemon(DaemonMode.INITIALIZE)
    with patch("claude_works.web.app._daemon_ref", daemon), \
         patch("claude_works.web.app._setup_token", None):
        async with client as c:
            r = await c.post("/api/setup/save", json={}, headers={"X-Setup-Token": "tok"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/setup/save — field validation
# ---------------------------------------------------------------------------

@pytest.fixture
def initialize_daemon():
    return _make_daemon(DaemonMode.INITIALIZE)


_valid_cfg = {
    "config": {
        "telegram": {"token": "bot123:ABC", "admin_chat_ids": [12345]},
        "web": {"auth_token": "supersecret"},
    }
}


@pytest.mark.asyncio
async def test_save_setup_400_missing_telegram_token(client, initialize_daemon):
    body = {"config": {"telegram": {"admin_chat_ids": [1]}, "web": {"auth_token": "x"}}}
    with patch("claude_works.web.app._daemon_ref", initialize_daemon), \
         patch("claude_works.web.app._setup_token", "tok"):
        async with client as c:
            r = await c.post("/api/setup/save", json=body, headers={"X-Setup-Token": "tok"})
    assert r.status_code == 400
    assert "telegram.token" in r.json()["detail"]


@pytest.mark.asyncio
async def test_save_setup_400_placeholder_telegram_token(client, initialize_daemon):
    body = {"config": {"telegram": {"token": "YOUR_BOT_TOKEN", "admin_chat_ids": [1]}, "web": {"auth_token": "x"}}}
    with patch("claude_works.web.app._daemon_ref", initialize_daemon), \
         patch("claude_works.web.app._setup_token", "tok"):
        async with client as c:
            r = await c.post("/api/setup/save", json=body, headers={"X-Setup-Token": "tok"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_save_setup_400_missing_auth_token(client, initialize_daemon):
    body = {"config": {"telegram": {"token": "bot:ABC", "admin_chat_ids": [1]}, "web": {}}}
    with patch("claude_works.web.app._daemon_ref", initialize_daemon), \
         patch("claude_works.web.app._setup_token", "tok"):
        async with client as c:
            r = await c.post("/api/setup/save", json=body, headers={"X-Setup-Token": "tok"})
    assert r.status_code == 400
    assert "web.auth_token" in r.json()["detail"]


@pytest.mark.asyncio
async def test_save_setup_400_missing_admin_ids(client, initialize_daemon):
    body = {"config": {"telegram": {"token": "bot:ABC"}, "web": {"auth_token": "x"}}}
    with patch("claude_works.web.app._daemon_ref", initialize_daemon), \
         patch("claude_works.web.app._setup_token", "tok"):
        async with client as c:
            r = await c.post("/api/setup/save", json=body, headers={"X-Setup-Token": "tok"})
    assert r.status_code == 400
    assert "admin_chat_ids" in r.json()["detail"]


# ---------------------------------------------------------------------------
# POST /api/setup/save — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_setup_happy_path(client, initialize_daemon):
    conn = await _make_conn()
    mock_init = AsyncMock(return_value=conn)

    with patch("claude_works.web.app._daemon_ref", initialize_daemon), \
         patch("claude_works.web.app._setup_token", "tok"), \
         patch("claude_works.web.app.db.init", mock_init):
        async with client as c:
            r = await c.post("/api/setup/save", json=_valid_cfg, headers={"X-Setup-Token": "tok"})

    await conn.close()
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.asyncio
async def test_save_setup_invalidates_token(client, initialize_daemon):
    import claude_works.web.app as web_app

    conn = await _make_conn()
    mock_init = AsyncMock(return_value=conn)

    with patch("claude_works.web.app._daemon_ref", initialize_daemon), \
         patch("claude_works.web.app._setup_token", "tok"), \
         patch("claude_works.web.app.db.init", mock_init):
        async with client as c:
            await c.post("/api/setup/save", json=_valid_cfg, headers={"X-Setup-Token": "tok"})
        assert web_app._setup_token is None

    await conn.close()
