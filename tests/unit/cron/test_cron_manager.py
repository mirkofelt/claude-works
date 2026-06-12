import json

import pytest

from claude_works import config, db
from claude_works.cron import CronJob, CronManager


@pytest.fixture
async def conn(tmp_path):
    c = await db.init(str(tmp_path / "test.db"))
    yield c
    await c.close()


@pytest.fixture
def notifications():
    return []


def _manager(conn, notifications, running=True):
    async def notify(msg):
        notifications.append(msg)
    return CronManager(conn=conn, notify=notify, is_running=lambda: running)


def _set_cron_config(jobs: dict):
    config.set({"cron": jobs})


async def test_due_job_runs_and_persists_state(conn, notifications):
    _set_cron_config({"j1": {"enabled": True, "interval_minutes": 5}})
    calls = []

    async def handler(ctx, state):
        calls.append(state)
        return {"counter": state.get("counter", 0) + 1}

    mgr = _manager(conn, notifications)
    mgr.register(CronJob(name="j1", handler=handler))
    await mgr._ensure_rows()
    await mgr._tick_job(mgr._jobs["j1"])

    assert calls == [{}]
    row = await mgr._load_row("j1")
    assert json.loads(row["state_json"]) == {"counter": 1}
    assert row["last_status"] == "ok"
    assert row["last_error"] is None
    assert row["last_run_at"] is not None


async def test_disabled_job_does_not_run(conn, notifications):
    _set_cron_config({"j1": {"enabled": False}})
    calls = []

    async def handler(ctx, state):
        calls.append(1)
        return state

    mgr = _manager(conn, notifications)
    mgr.register(CronJob(name="j1", handler=handler, default_enabled=False))
    await mgr._ensure_rows()
    await mgr._tick_job(mgr._jobs["j1"])
    assert calls == []


async def test_interval_respected(conn, notifications):
    _set_cron_config({"j1": {"enabled": True, "interval_minutes": 5}})
    calls = []

    async def handler(ctx, state):
        calls.append(1)
        return state

    mgr = _manager(conn, notifications)
    mgr.register(CronJob(name="j1", handler=handler))
    await mgr._ensure_rows()
    await mgr._tick_job(mgr._jobs["j1"])  # runs
    await mgr._tick_job(mgr._jobs["j1"])  # within interval → skip
    assert calls == [1]


async def test_state_survives_new_manager_instance(conn, notifications):
    """Durability: state is read from DB, not memory — restart-safe."""
    _set_cron_config({"j1": {"enabled": True}})

    async def handler(ctx, state):
        return {"counter": state.get("counter", 0) + 1}

    mgr1 = _manager(conn, notifications)
    mgr1.register(CronJob(name="j1", handler=handler))
    await mgr1._ensure_rows()
    await mgr1._run_job(mgr1._jobs["j1"], await mgr1._load_row("j1"))

    mgr2 = _manager(conn, notifications)  # simulated restart
    mgr2.register(CronJob(name="j1", handler=handler))
    await mgr2._ensure_rows()  # INSERT OR IGNORE — must not reset state
    await mgr2._run_job(mgr2._jobs["j1"], await mgr2._load_row("j1"))

    row = await mgr2._load_row("j1")
    assert json.loads(row["state_json"]) == {"counter": 2}


async def test_error_notifies_once_per_distinct_error(conn, notifications):
    _set_cron_config({"j1": {"enabled": True}})
    errors = ["boom", "boom", "different"]

    async def handler(ctx, state):
        raise RuntimeError(errors.pop(0))

    mgr = _manager(conn, notifications)
    mgr.register(CronJob(name="j1", handler=handler))
    await mgr._ensure_rows()
    for _ in range(3):
        await mgr._run_job(mgr._jobs["j1"], await mgr._load_row("j1"))

    assert len(notifications) == 2  # "boom" once, "different" once
    assert "boom" in notifications[0]
    assert "different" in notifications[1]
    row = await mgr._load_row("j1")
    assert row["last_status"] == "error"


async def test_mid_run_save_state_persists_even_on_later_error(conn, notifications):
    """save_state(ctx) must persist immediately — even if the handler fails afterwards."""
    _set_cron_config({"j1": {"enabled": True}})

    async def handler(ctx, state):
        await ctx.save_state({"baseline": "abc"})
        raise RuntimeError("deploy failed")

    mgr = _manager(conn, notifications)
    mgr.register(CronJob(name="j1", handler=handler))
    await mgr._ensure_rows()
    await mgr._run_job(mgr._jobs["j1"], await mgr._load_row("j1"))

    row = await mgr._load_row("j1")
    assert json.loads(row["state_json"]) == {"baseline": "abc"}
    assert row["last_status"] == "error"
    assert len(notifications) == 1


async def test_interval_config_resolution(conn, notifications):
    _set_cron_config({"j1": {"enabled": True, "interval_minutes": 10}})
    mgr = _manager(conn, notifications)
    job = CronJob(name="j1", handler=None, default_interval_seconds=300)
    mgr.register(job)
    assert mgr._interval(job) == 600

    _set_cron_config({"j1": {"enabled": True}})
    assert mgr._interval(job) == 300  # default

    _set_cron_config({"j1": {"enabled": True, "interval_seconds": 5}})
    assert mgr._interval(job) == 60  # floor at 60s
