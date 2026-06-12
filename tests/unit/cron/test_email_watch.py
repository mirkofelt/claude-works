import pytest

from claude_works import config, db
from claude_works.cron import CronContext
from claude_works.tasks import email_watch as ew


@pytest.fixture
async def conn(tmp_path):
    c = await db.init(str(tmp_path / "test.db"))
    yield c
    await c.close()


@pytest.fixture
def env(conn):
    config.set({
        "llm": {"provider": "api", "api_key": "k"},
        "agents": {},
        "email": {"imap_host": "imap.test", "imap_user": "u", "imap_password": "p"},
        "cron": {"email_watch": {"enabled": True}},
    })
    rec = {"notify": [], "saved": []}

    async def notify(msg):
        rec["notify"].append(msg)

    async def save_state(state):
        rec["saved"].append(dict(state))

    ctx = CronContext(
        conn=conn,
        job_cfg=config.section("cron")["email_watch"],
        notify=notify,
        save_state=save_state,
    )
    return ctx, rec


def _mock_read(monkeypatch, max_uid, messages):
    async def fake_read(folder, since_uid, max_count, snippet_chars, cfg):
        return {"max_uid": max_uid, "messages": messages}
    monkeypatch.setattr(ew, "read_new_emails", fake_read)


class _FakeProvider:
    """complete() returns canned JSON per call; close() is a no-op recorder."""
    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.closed = False
        self.calls = 0

    async def complete(self, messages, *, system, model, max_tokens, mcp_servers=None):
        self.calls += 1
        v = self._verdicts.pop(0)
        if isinstance(v, Exception):
            raise v

        class _R:
            text = v
        return _R()

    async def close(self):
        self.closed = True


def _mock_provider(monkeypatch, verdicts):
    fake = _FakeProvider(verdicts)
    monkeypatch.setattr(ew, "get_provider", lambda cfg: fake)
    return fake


def _msg(uid, frm="a@b.de", subject="Hi", snippet="text"):
    return {"uid": uid, "from": frm, "subject": subject, "date": "", "snippet": snippet}


async def test_first_run_arms_baseline_no_flood(env, monkeypatch):
    ctx, rec = env
    _mock_read(monkeypatch, max_uid=42, messages=[])

    state = await ew.email_watch(ctx, {})

    assert state["last_uid"] == 42
    assert len(rec["notify"]) == 1 and "Baseline" in rec["notify"][0]


async def test_no_new_messages_is_silent(env, monkeypatch):
    ctx, rec = env
    _mock_read(monkeypatch, max_uid=50, messages=[])

    state = await ew.email_watch(ctx, {"last_uid": 50})

    assert state["last_uid"] == 50
    assert rec["notify"] == []


async def test_relevance_filter_off_forwards_all(env, monkeypatch):
    ctx, rec = env
    ctx.job_cfg["relevance_filter"] = False
    _mock_read(monkeypatch, max_uid=12, messages=[_msg(11), _msg(12)])
    fake = _mock_provider(monkeypatch, [])  # must not be called

    state = await ew.email_watch(ctx, {"last_uid": 10})

    assert state["last_uid"] == 12
    assert len(rec["notify"]) == 1
    assert "2 neue" in rec["notify"][0]
    assert fake.calls == 0


async def test_relevance_filter_keeps_only_relevant(env, monkeypatch):
    ctx, rec = env
    _mock_read(monkeypatch, max_uid=12, messages=[
        _msg(11, subject="Rechnung fällig"),
        _msg(12, subject="Newsletter"),
    ])
    _mock_provider(monkeypatch, [
        '{"relevant": true, "reason": "Rechnung mit Frist"}',
        '{"relevant": false, "reason": "Newsletter"}',
    ])

    state = await ew.email_watch(ctx, {"last_uid": 10})

    assert state["last_uid"] == 12
    assert len(rec["notify"]) == 1
    digest = rec["notify"][0]
    assert "1 relevante" in digest
    assert "Rechnung fällig" in digest
    assert "Newsletter" not in digest


async def test_all_irrelevant_is_silent_but_advances(env, monkeypatch):
    ctx, rec = env
    _mock_read(monkeypatch, max_uid=12, messages=[_msg(12, subject="Spam")])
    _mock_provider(monkeypatch, ['{"relevant": false, "reason": "Spam"}'])

    state = await ew.email_watch(ctx, {"last_uid": 11})

    assert state["last_uid"] == 12
    assert rec["notify"] == []


async def test_llm_error_fails_open(env, monkeypatch):
    ctx, rec = env
    _mock_read(monkeypatch, max_uid=12, messages=[_msg(12, subject="Unsicher")])
    _mock_provider(monkeypatch, [RuntimeError("LLM down")])

    state = await ew.email_watch(ctx, {"last_uid": 11})

    assert state["last_uid"] == 12
    assert len(rec["notify"]) == 1
    assert "Unsicher" in rec["notify"][0]
    assert "Filter-Fehler" in rec["notify"][0]


async def test_notify_failure_does_not_advance(env, monkeypatch, conn):
    ctx, rec = env
    _mock_read(monkeypatch, max_uid=12, messages=[_msg(12, subject="Rechnung")])
    _mock_provider(monkeypatch, ['{"relevant": true, "reason": "wichtig"}'])

    async def boom(msg):
        raise RuntimeError("telegram down")
    ctx.notify = boom

    # Handler must propagate so CronManager skips saving state → mail re-evaluated next tick.
    with pytest.raises(RuntimeError, match="telegram down"):
        await ew.email_watch(ctx, {"last_uid": 11})


async def test_missing_imap_host_raises(env, monkeypatch):
    ctx, rec = env
    config.set({**config.get(), "email": {}})

    with pytest.raises(RuntimeError, match="imap_host"):
        await ew.email_watch(ctx, {"last_uid": 5})
