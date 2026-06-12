import asyncio
import time

import pytest

import claude_works.agents.heartbeat as hb_mod
from claude_works.agents.heartbeat import Heartbeat, HeartbeatTimeout, run_with_heartbeat


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Speed up the supervisor poll so tests run in milliseconds."""
    monkeypatch.setattr(hb_mod, "_POLL_SECONDS", 0.02)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_beat_resets_idle_seconds():
    hb = Heartbeat()
    await asyncio.sleep(0.05)
    assert hb.idle_seconds >= 0.04
    hb.beat()
    assert hb.idle_seconds < 0.04


def test_heartbeat_timeout_is_timeout_error():
    # must be catchable by existing `except asyncio.TimeoutError` handlers
    assert issubclass(HeartbeatTimeout, asyncio.TimeoutError)


# ---------------------------------------------------------------------------
# run_with_heartbeat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_result_on_completion():
    hb = Heartbeat()

    async def work():
        return 42

    assert await run_with_heartbeat(work(), hb, idle_timeout=1.0) == 42


@pytest.mark.asyncio
async def test_idle_timeout_cancels_silent_task():
    hb = Heartbeat()
    cancelled = False

    async def work():
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise

    with pytest.raises(HeartbeatTimeout):
        await run_with_heartbeat(work(), hb, idle_timeout=0.05)
    assert cancelled


@pytest.mark.asyncio
async def test_activity_resets_idle_timer():
    """Total runtime exceeds idle_timeout — survives only because of beats."""
    hb = Heartbeat()

    async def work():
        for _ in range(10):
            await asyncio.sleep(0.03)
            hb.beat()
        return "done"

    assert await run_with_heartbeat(work(), hb, idle_timeout=0.1) == "done"


@pytest.mark.asyncio
async def test_deadline_fires_despite_constant_activity():
    hb = Heartbeat()

    async def work():
        while True:
            await asyncio.sleep(0.01)
            hb.beat()

    with pytest.raises(HeartbeatTimeout):
        await run_with_heartbeat(
            work(), hb, idle_timeout=5.0, deadline=time.monotonic() + 0.1
        )


@pytest.mark.asyncio
async def test_exception_propagates_unchanged():
    hb = Heartbeat()

    async def work():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await run_with_heartbeat(work(), hb, idle_timeout=1.0)


# ---------------------------------------------------------------------------
# Provider in-flight beats
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provider_beats_while_call_in_flight(monkeypatch):
    from claude_works.llm import provider as prov_mod

    monkeypatch.setattr(prov_mod, "_HEARTBEAT_INTERVAL", 0.02)
    beats: list[int] = []

    async def slow_call():
        await asyncio.sleep(0.1)
        return "x"

    out = await prov_mod._beat_while_running(slow_call(), lambda: beats.append(1))
    assert out == "x"
    assert len(beats) >= 2  # emitted periodically while in flight


@pytest.mark.asyncio
async def test_provider_no_heartbeat_callback_passthrough():
    from claude_works.llm import provider as prov_mod

    async def call():
        return "y"

    assert await prov_mod._beat_while_running(call(), None) == "y"
