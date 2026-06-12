import time
import pytest
import aiosqlite
import claude_works.config as cfg
from claude_works.telemetry.tokens import BudgetExceededError, TokenTracker
from claude_works.db import CREATE_TABLES


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
async def test_estimate_cost_includes_cache_tokens(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    # sonnet: 3.00 in + 15.00 out + 0.30 cache-read + 3.75 cache-write per MTok
    cost = cfg.estimate_cost(
        "claude-sonnet-4-6", 1_000_000, 1_000_000,
        cache_read_tokens=1_000_000, cache_write_tokens=1_000_000,
    )
    assert abs(cost - 22.05) < 0.001


@pytest.mark.asyncio
async def test_estimate_cost_cache_fallback_multipliers(monkeypatch):
    # custom pricing without explicit cache rates → 0.1x / 1.25x of input
    monkeypatch.setattr(cfg, "_settings", {
        "spending": {"model_pricing": {
            "custom-model": {"input_per_mtok": 10.0, "output_per_mtok": 50.0}
        }}
    })
    cost = cfg.estimate_cost(
        "custom-model", 0, 0,
        cache_read_tokens=1_000_000, cache_write_tokens=1_000_000,
    )
    assert abs(cost - (1.0 + 12.5)) < 0.001


@pytest.mark.asyncio
async def test_estimate_cost_unknown_model_zero(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    assert cfg.estimate_cost("no-such-model", 1_000_000, 1_000_000) == 0.0


@pytest.mark.asyncio
async def test_tracker_logs_cache_cost(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    tracker = await _make_tracker()
    await tracker.log(
        agent_id="test", agent_class="generalist", task_id=1,
        user_id=1, chat_id=1, model="claude-sonnet-4-6",
        input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000, cache_write_tokens=0,
    )
    cost = await tracker.total_cost()
    assert abs(cost - 0.30) < 0.001


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


@pytest.mark.asyncio
async def test_per_user_daily_limit_rejects(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {
        "spending": {"per_user_daily_usd": 0.001}
    })
    tracker = await _make_tracker()
    await tracker.log(
        agent_id="test", agent_class="generalist", task_id=1,
        user_id=42, chat_id=1, model="claude-sonnet-4-6",
        input_tokens=100_000, output_tokens=10_000,
    )
    result = await tracker.get_allowed_model("claude-sonnet-4-6", user_id=42)
    assert result is None


@pytest.mark.asyncio
async def test_per_user_limit_other_user_unaffected(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {
        "spending": {"per_user_daily_usd": 0.001}
    })
    tracker = await _make_tracker()
    await tracker.log(
        agent_id="test", agent_class="generalist", task_id=1,
        user_id=42, chat_id=1, model="claude-sonnet-4-6",
        input_tokens=100_000, output_tokens=10_000,
    )
    # user 99 has spent nothing — should pass
    result = await tracker.get_allowed_model("claude-sonnet-4-6", user_id=99)
    assert result == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_per_user_limit_no_user_id_skips_check(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {
        "spending": {"per_user_daily_usd": 0.001}
    })
    tracker = await _make_tracker()
    await tracker.log(
        agent_id="test", agent_class="generalist", task_id=1,
        user_id=42, chat_id=1, model="claude-sonnet-4-6",
        input_tokens=100_000, output_tokens=10_000,
    )
    # no user_id → per-user check skipped
    result = await tracker.get_allowed_model("claude-sonnet-4-6", user_id=None)
    assert result == "claude-sonnet-4-6"
