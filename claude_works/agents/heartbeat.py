"""Heartbeat-based task supervision.

Replaces pure wall-clock kills: a running agent emits heartbeats whenever it
makes progress (LLM call in flight, tool loop iteration, compaction). The
supervisor cancels a task only when heartbeats stop for longer than
``agent.idle_timeout_seconds`` — or when the absolute runtime cap
(``agent.max_runtime_seconds`` for board tasks, ``agent.reply_timeout_seconds``
for inline chat runs) is exceeded (zombie protection).
"""

import asyncio
import logging
import time
from typing import Awaitable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_POLL_SECONDS = 5.0


class HeartbeatTimeout(asyncio.TimeoutError):
    """Task cancelled by the heartbeat supervisor. Subclasses TimeoutError so
    existing ``except asyncio.TimeoutError`` handlers keep working."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Heartbeat:
    """Monotonic life-sign tracker. ``beat()`` is sync and lock-free —
    safe to call from any coroutine of the same event loop."""

    def __init__(self) -> None:
        self._last = time.monotonic()

    def beat(self) -> None:
        self._last = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last


async def run_with_heartbeat(
    coro: Awaitable[T],
    heartbeat: Heartbeat,
    idle_timeout: float,
    deadline: float | None = None,
) -> T:
    """Await ``coro``; cancel it only when heartbeats stop.

    - ``idle_timeout``: seconds without a heartbeat before the task is killed.
    - ``deadline``: absolute ``time.monotonic()`` value as hard runtime cap
      (None = no cap). Spans multiple calls when the caller reuses one deadline.

    Raises :class:`HeartbeatTimeout` on either condition; exceptions from
    ``coro`` propagate unchanged.
    """
    heartbeat.beat()  # starting counts as a life sign
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_POLL_SECONDS)
            if done:
                return task.result()
            idle = heartbeat.idle_seconds
            if idle > idle_timeout:
                reason = f"no heartbeat for {idle:.0f}s (limit {idle_timeout:.0f}s)"
                logger.error("Heartbeat supervisor: %s — cancelling task", reason)
                raise HeartbeatTimeout(reason)
            if deadline is not None and time.monotonic() > deadline:
                reason = "max runtime exceeded (hard cap reached)"
                logger.error("Heartbeat supervisor: %s — cancelling task", reason)
                raise HeartbeatTimeout(reason)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 — already failing, swallow cleanup errors
                pass
