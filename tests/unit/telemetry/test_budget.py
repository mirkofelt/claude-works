import time
import pytest
import aiosqlite
import comms.config as cfg
from comms.telemetry.tokens import BudgetExceededError, TokenTracker
from comms.db import CREATE_TABLES


async def _make_tracker() -> TokenTracker:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CREATE_TABLES)
    await conn.commit()
    return TokenTracker(conn)


async def _log(tracker: TokenTracker, model: str, inp: int, out: int) -> None:
    await tracker.log(
        agent_id="test", agent_class="generalist", task_id=1,
        user_id=1, chat_id=1, model=model,
        input_tokens=inp, output_tokens=out,
    )


@pytest.mark.asyncio
async def test_estimate_cost(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    # sonnet: 3.00 input + 15.00 output per MTok
    cost = cfg.estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert abs(cost - 18.0) < 0.001


@pytest.mark.asyncio
async def test_no_limits_always_allowed(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    tracker = await _make_tracker()
    result = await tracker.get_allowed_model("claude-sonnet-4-6")
    assert result == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_daily_limit_reject(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {
        "spending": {"max_daily_usd": 0.001, "on_limit_exceeded": "reject"}
    })
    tracker = await _make_tracker()
    await _log(tracker, "claude-sonnet-4-6", 100_000, 10_000)  # ~$0.45 → over $0.001
    result = await tracker.get_allowed_model("claude-sonnet-4-6")
    assert result is None


@pytest.mark.asyncio
async def test_daily_limit_downgrade(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {
        "spending": {"max_daily_usd": 0.001, "on_limit_exceeded": "downgrade"}
    })
    tracker = await _make_tracker()
    await _log(tracker, "claude-sonnet-4-6", 100_000, 10_000)
    # balanced → downgrade to fast
    result = await tracker.get_allowed_model("claude-sonnet-4-6")
    assert result == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_downgrade_already_cheapest_rejects(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {
        "spending": {"max_daily_usd": 0.001, "on_limit_exceeded": "downgrade"}
    })
    tracker = await _make_tracker()
    await _log(tracker, "claude-haiku-4-5-20251001", 100_000, 10_000)
    # haiku is already fast tier → no cheaper → returns None
    result = await tracker.get_allowed_model("claude-haiku-4-5-20251001")
    assert result is None


@pytest.mark.asyncio
async def test_under_limit_passes(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {
        "spending": {"max_daily_usd": 100.0, "on_limit_exceeded": "reject"}
    })
    tracker = await _make_tracker()
    await _log(tracker, "claude-sonnet-4-6", 100, 100)  # tiny cost
    result = await tracker.get_allowed_model("claude-sonnet-4-6")
    assert result == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_cost_logged_in_db(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    tracker = await _make_tracker()
    await _log(tracker, "claude-sonnet-4-6", 1_000_000, 0)
    cost = await tracker.total_cost()
    assert abs(cost - 3.0) < 0.001


@pytest.mark.asyncio
async def test_downgrade_model_chain(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    assert cfg.downgrade_model("claude-opus-4-8") == "claude-sonnet-4-6"
    assert cfg.downgrade_model("claude-sonnet-4-6") == "claude-haiku-4-5-20251001"
    assert cfg.downgrade_model("claude-haiku-4-5-20251001") is None
