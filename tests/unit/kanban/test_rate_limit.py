import asyncio
import time
import pytest
import aiosqlite

from claude_works.db import CREATE_TABLES
from claude_works.kanban.board import KanbanBoard
from claude_works.kanban.models import AgentClass, KanbanTask, Lane
from claude_works.llm.errors import RateLimitError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_board() -> KanbanBoard:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CREATE_TABLES)
    await conn.commit()
    return KanbanBoard(conn)


async def _push_assigned(board: KanbanBoard, content: str = "do thing") -> int:
    task = KanbanTask(id=None, chat_id=1, user_id=1, content=content, priority=0)
    task_id = await board.push(task)
    await board.assign(task_id, AgentClass.GENERALIST)
    return task_id


# ---------------------------------------------------------------------------
# RateLimitError
# ---------------------------------------------------------------------------

def test_rate_limit_error_no_retry():
    err = RateLimitError("hit limit")
    assert str(err) == "hit limit"
    assert err.retry_after is None


def test_rate_limit_error_with_retry():
    err = RateLimitError("hit limit", retry_after=60.0)
    assert err.retry_after == 60.0


# ---------------------------------------------------------------------------
# KanbanBoard.requeue
# ---------------------------------------------------------------------------

@pytest.fixture
async def board():
    b = await _make_board()
    yield b
    await b._conn.close()


@pytest.mark.asyncio
async def test_requeue_moves_in_progress_to_assigned(board):
    task_id = await _push_assigned(board)

    # move to IN_PROGRESS
    started = await board.start(task_id, "agent-1")
    assert started

    task_before = await board.get(task_id)
    assert task_before.lane == Lane.IN_PROGRESS
    assert task_before.agent_id == "agent-1"
    assert task_before.started_at is not None

    await board.requeue(task_id)

    task_after = await board.get(task_id)
    assert task_after.lane == Lane.ASSIGNED
    assert task_after.agent_id is None
    assert task_after.started_at is None


@pytest.mark.asyncio
async def test_requeue_noop_if_not_in_progress(board):
    task_id = await _push_assigned(board)
    # still ASSIGNED — requeue should not change anything
    await board.requeue(task_id)
    task = await board.get(task_id)
    assert task.lane == Lane.ASSIGNED


@pytest.mark.asyncio
async def test_requeue_noop_if_done(board):
    task_id = await _push_assigned(board)
    await board.start(task_id, "agent-1")
    await board.complete(task_id, "result text")
    await board.requeue(task_id)
    task = await board.get(task_id)
    assert task.lane == Lane.DONE  # unchanged


@pytest.mark.asyncio
async def test_requeue_sets_notify(board):
    """requeue() should fire the notify event so waiting loops wake up."""
    task_id = await _push_assigned(board)
    await board.start(task_id, "agent-1")
    board._notify.clear()
    await board.requeue(task_id)
    assert board._notify.is_set()


# ---------------------------------------------------------------------------
# Exponential backoff formula
# ---------------------------------------------------------------------------

def _backoff(retry_after: float | None, hit_count: int) -> float:
    """Mirror the formula in coordinator._run_specialist."""
    base = retry_after or 30.0
    return min(base * (2 ** (hit_count - 1)), 900.0)


def test_backoff_first_hit_equals_base():
    assert _backoff(None, 1) == 30.0
    assert _backoff(60.0, 1) == 60.0


def test_backoff_doubles_per_hit():
    assert _backoff(30.0, 2) == 60.0
    assert _backoff(30.0, 3) == 120.0
    assert _backoff(30.0, 4) == 240.0


def test_backoff_caps_at_900():
    assert _backoff(30.0, 20) == 900.0
    assert _backoff(1000.0, 1) == 900.0


# ---------------------------------------------------------------------------
# Coordinator rate-limit state (property tests, no async I/O)
# ---------------------------------------------------------------------------

def _make_coordinator_stub():
    """Return a bare coordinator with mocked deps for property tests."""
    from unittest.mock import MagicMock
    from claude_works.agents.coordinator import AgentCoordinator

    board = MagicMock()
    knowledge = MagicMock()
    token_tracker = MagicMock()
    on_result = MagicMock()
    coord = AgentCoordinator(board, knowledge, token_tracker, on_result)
    return coord


def test_is_rate_limited_false_initially():
    coord = _make_coordinator_stub()
    assert not coord.is_rate_limited
    assert coord.rate_limit_until is None


def test_is_rate_limited_true_when_future():
    coord = _make_coordinator_stub()
    coord._rate_limit_until = time.time() + 60.0
    assert coord.is_rate_limited
    assert coord.rate_limit_until is not None


def test_is_rate_limited_false_after_expiry():
    coord = _make_coordinator_stub()
    coord._rate_limit_until = time.time() - 1.0  # in the past
    assert not coord.is_rate_limited
    assert coord.rate_limit_until is None
