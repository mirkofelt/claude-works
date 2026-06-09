import pytest
import aiosqlite

from comms.db import CONFIG_TABLES
from comms.config_store import delete_config, load_config, save_config


async def _make_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CONFIG_TABLES)
    await conn.commit()
    return conn


@pytest.fixture
async def conn():
    c = await _make_conn()
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_load_returns_none_on_empty(conn):
    result = await load_config(conn)
    assert result is None


@pytest.mark.asyncio
async def test_save_and_load_roundtrip(conn):
    cfg = {"telegram": {"token": "abc"}, "web": {"auth_token": "xyz"}}
    await save_config(conn, cfg)
    result = await load_config(conn)
    assert result == cfg


@pytest.mark.asyncio
async def test_save_upserts(conn):
    await save_config(conn, {"v": 1})
    await save_config(conn, {"v": 2})
    result = await load_config(conn)
    assert result == {"v": 2}


@pytest.mark.asyncio
async def test_delete_removes_config(conn):
    await save_config(conn, {"v": 1})
    await delete_config(conn)
    result = await load_config(conn)
    assert result is None


@pytest.mark.asyncio
async def test_delete_noop_on_empty(conn):
    await delete_config(conn)
    result = await load_config(conn)
    assert result is None


@pytest.mark.asyncio
async def test_save_sets_updated_at(conn):
    await save_config(conn, {"v": 1})
    async with conn.execute("SELECT updated_at FROM daemon_config WHERE id = 1") as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row["updated_at"] > 0
