import pytest

from claude_works import config, db
from claude_works.cron import CronContext
from claude_works.tasks import deploy_watch as dw

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
async def conn(tmp_path):
    c = await db.init(str(tmp_path / "test.db"))
    yield c
    await c.close()


@pytest.fixture
def env(conn):
    """CronContext + recorders for notify/save_state/deploy."""
    config.set({
        "github": {"token": "t", "gh_binary": "gh"},
        "system": {"claude_guard": {"url": "http://guard:9876", "token": "x"}},
        "cron": {"deploy_watch": {"enabled": True, "repo": "o/r", "branch": "main"}},
    })
    rec = {"notify": [], "saved": [], "deploys": 0}

    async def notify(msg):
        rec["notify"].append(msg)

    async def save_state(state):
        rec["saved"].append(dict(state))

    ctx = CronContext(
        conn=conn,
        job_cfg={"repo": "o/r", "branch": "main"},
        notify=notify,
        save_state=save_state,
    )
    return ctx, rec


def _mock_github(monkeypatch, sha, message="feat: something"):
    async def fake_api(method, endpoint, body, cfg):
        assert method == "GET"
        assert endpoint == "/repos/o/r/commits/main"
        return {"sha": sha, "commit": {"message": message}}
    monkeypatch.setattr(dw, "github_api", fake_api)


def _mock_deploy(monkeypatch, rec, fail=False):
    async def fake_deploy():
        if fail:
            raise RuntimeError("claude-guard /deploy HTTP 500: kaputt")
        rec["deploys"] += 1
    monkeypatch.setattr(dw, "_trigger_deploy", fake_deploy)


async def _kb_content(conn):
    async with conn.execute(
        "SELECT content FROM knowledge WHERE title = ?", (dw.KB_TITLE,)
    ) as cur:
        row = await cur.fetchone()
    return row["content"] if row else None


async def test_first_run_seeds_baseline_without_deploy(env, monkeypatch, conn):
    ctx, rec = env
    _mock_github(monkeypatch, SHA_A)
    _mock_deploy(monkeypatch, rec)

    state = await dw.deploy_watch(ctx, {})

    assert state["baseline_sha"] == SHA_A
    assert rec["deploys"] == 0
    assert len(rec["notify"]) == 1 and "Baseline" in rec["notify"][0]
    assert SHA_A in (await _kb_content(conn) or "")


async def test_same_sha_is_silent(env, monkeypatch):
    ctx, rec = env
    _mock_github(monkeypatch, SHA_A)
    _mock_deploy(monkeypatch, rec)

    state = await dw.deploy_watch(ctx, {"baseline_sha": SHA_A})

    assert state["baseline_sha"] == SHA_A
    assert rec["notify"] == []
    assert rec["saved"] == []
    assert rec["deploys"] == 0


async def test_new_sha_triggers_deploy_and_updates_baseline(env, monkeypatch, conn):
    ctx, rec = env
    _mock_github(monkeypatch, SHA_B, message="fix: wichtiger bugfix\n\nDetails...")
    _mock_deploy(monkeypatch, rec)

    state = await dw.deploy_watch(ctx, {"baseline_sha": SHA_A})

    assert state["baseline_sha"] == SHA_B
    assert rec["deploys"] == 1
    # baseline persisted BEFORE deploy trigger (restart race)
    assert rec["saved"] == [{"baseline_sha": SHA_B}]
    assert len(rec["notify"]) == 1
    assert SHA_B[:7] in rec["notify"][0]
    assert "wichtiger bugfix" in rec["notify"][0]
    assert SHA_B in (await _kb_content(conn) or "")


async def test_deploy_failure_raises_but_baseline_already_saved(env, monkeypatch):
    ctx, rec = env
    _mock_github(monkeypatch, SHA_B)
    _mock_deploy(monkeypatch, rec, fail=True)

    with pytest.raises(RuntimeError, match="claude-guard"):
        await dw.deploy_watch(ctx, {"baseline_sha": SHA_A})

    # No silent retry loop: baseline saved first, error propagates to CronManager → notification
    assert rec["saved"] == [{"baseline_sha": SHA_B}]


async def test_missing_sha_raises(env, monkeypatch):
    ctx, rec = env

    async def fake_api(method, endpoint, body, cfg):
        return {}
    monkeypatch.setattr(dw, "github_api", fake_api)

    with pytest.raises(RuntimeError, match="SHA"):
        await dw.deploy_watch(ctx, {"baseline_sha": SHA_A})


async def test_kb_entry_updated_not_duplicated(env, monkeypatch, conn):
    ctx, rec = env
    _mock_deploy(monkeypatch, rec)

    _mock_github(monkeypatch, SHA_A)
    await dw.deploy_watch(ctx, {})
    _mock_github(monkeypatch, SHA_B)
    await dw.deploy_watch(ctx, {"baseline_sha": SHA_A})

    async with conn.execute(
        "SELECT COUNT(*) FROM knowledge WHERE title = ?", (dw.KB_TITLE,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 1
    assert SHA_B in (await _kb_content(conn) or "")
