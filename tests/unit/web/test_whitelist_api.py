"""Tests for /api/whitelist endpoints incl. meta-protection (approval gate)."""
import pytest
import aiosqlite
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient, ASGITransport

import claude_works.config as config
from claude_works.db import CREATE_TABLES, CONFIG_TABLES
from claude_works.web.app import app, _verify_token


@pytest.fixture(autouse=True)
def _auth_and_config():
    # Bypass token auth for these tests; reset config to a known baseline.
    app.dependency_overrides[_verify_token] = lambda: None
    config.set({"security": {"enabled": True}, "whitelist": {"rules": []}})
    yield
    app.dependency_overrides.pop(_verify_token, None)


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _mem_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CREATE_TABLES)
    await conn.executescript(CONFIG_TABLES)
    await conn.commit()
    return conn


def _daemon_with_approval(approved: bool):
    daemon = MagicMock()
    daemon._security = MagicMock()
    daemon._security.require_approval = AsyncMock(return_value=approved)
    return daemon


# --- GET --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_empty(client):
    async with client as c:
        r = await c.get("/api/whitelist")
    assert r.status_code == 200
    assert r.json() == {"rules": []}


# --- POST: validation -------------------------------------------------------

@pytest.mark.asyncio
async def test_post_rejects_unknown_type(client):
    daemon = _daemon_with_approval(True)
    with patch("claude_works.web.state.daemon_ref", daemon):
        async with client as c:
            r = await c.post("/api/whitelist", json={"type": "nope", "matcher": {"x": "y"}})
    assert r.status_code == 400
    daemon._security.require_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_rejects_empty_matcher(client):
    daemon = _daemon_with_approval(True)
    with patch("claude_works.web.state.daemon_ref", daemon):
        async with client as c:
            r = await c.post("/api/whitelist", json={"type": "send_email", "matcher": {}})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_post_rejects_unknown_matcher_field(client):
    daemon = _daemon_with_approval(True)
    with patch("claude_works.web.state.daemon_ref", daemon):
        async with client as c:
            r = await c.post("/api/whitelist", json={"type": "send_email", "matcher": {"endpoint": "x"}})
    assert r.status_code == 400


# --- POST: meta-protection --------------------------------------------------

@pytest.mark.asyncio
async def test_post_denied_by_supervisor_returns_403(client):
    daemon = _daemon_with_approval(False)
    with patch("claude_works.web.state.daemon_ref", daemon):
        async with client as c:
            r = await c.post("/api/whitelist", json={"type": "send_email", "matcher": {"domain": "example.com"}})
    assert r.status_code == 403
    daemon._security.require_approval.assert_awaited_once()
    # Nothing persisted.
    assert config.section("whitelist").get("rules") == []


@pytest.mark.asyncio
async def test_post_no_security_returns_503(client):
    daemon = MagicMock()
    daemon._security = None
    with patch("claude_works.web.state.daemon_ref", daemon):
        async with client as c:
            r = await c.post("/api/whitelist", json={"type": "send_email", "matcher": {"domain": "example.com"}})
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_post_approved_persists_rule(client):
    daemon = _daemon_with_approval(True)
    conn = await _mem_conn()
    with patch("claude_works.web.state.daemon_ref", daemon), \
         patch("claude_works.web.routes.config.db.init_config", AsyncMock(return_value=conn)):
        async with client as c:
            r = await c.post("/api/whitelist", json={
                "type": "github_merge",
                "matcher": {"repo": "o/r", "branch": "feature/*"},
            })
    await conn.close()
    assert r.status_code == 200
    rule = r.json()["rule"]
    assert rule["type"] == "github_merge"
    assert rule["id"]
    assert config.section("whitelist")["rules"][0]["matcher"]["branch"] == "feature/*"


# --- DELETE -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_unknown_returns_404(client):
    daemon = _daemon_with_approval(True)
    with patch("claude_works.web.state.daemon_ref", daemon):
        async with client as c:
            r = await c.delete("/api/whitelist/deadbeef")
    assert r.status_code == 404
    daemon._security.require_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_requires_approval_and_removes(client):
    config.set({"security": {"enabled": True}, "whitelist": {"rules": [
        {"id": "abc123", "type": "send_email", "matcher": {"domain": "example.com"}, "enabled": True},
    ]}})
    daemon = _daemon_with_approval(True)
    conn = await _mem_conn()
    with patch("claude_works.web.state.daemon_ref", daemon), \
         patch("claude_works.web.routes.config.db.init_config", AsyncMock(return_value=conn)):
        async with client as c:
            r = await c.delete("/api/whitelist/abc123")
    await conn.close()
    assert r.status_code == 200
    daemon._security.require_approval.assert_awaited_once()
    assert config.section("whitelist")["rules"] == []


@pytest.mark.asyncio
async def test_delete_denied_keeps_rule(client):
    config.set({"security": {"enabled": True}, "whitelist": {"rules": [
        {"id": "abc123", "type": "send_email", "matcher": {"domain": "example.com"}, "enabled": True},
    ]}})
    daemon = _daemon_with_approval(False)
    with patch("claude_works.web.state.daemon_ref", daemon):
        async with client as c:
            r = await c.delete("/api/whitelist/abc123")
    assert r.status_code == 403
    assert len(config.section("whitelist")["rules"]) == 1
