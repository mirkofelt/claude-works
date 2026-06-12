import pytest
import aiosqlite
from unittest.mock import AsyncMock

from claude_works.db import CREATE_TABLES
from claude_works.kanban.board import (
    OFFLOAD_MARKER,
    KanbanBoard,
    build_offload_content,
    is_offloaded,
)
from claude_works.kanban.models import KanbanTask, Lane


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_board() -> KanbanBoard:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(CREATE_TABLES)
    await conn.commit()
    return KanbanBoard(conn)


@pytest.fixture
async def board():
    b = await _make_board()
    yield b
    await b._conn.close()


def _make_daemon(board):
    """Bare Daemon with mocked Telegram API — only offload path is exercised."""
    from claude_works.main import Daemon

    d = Daemon.__new__(Daemon)
    d._board = board
    d._api = AsyncMock()
    return d


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------

def test_build_offload_content_contains_marker_and_original():
    out = build_offload_content("KB komplett auditieren", 300.0)
    assert OFFLOAD_MARKER in out
    assert "300s" in out
    assert "KB komplett auditieren" in out


def test_is_offloaded_roundtrip():
    assert not is_offloaded("KB komplett auditieren")
    assert is_offloaded(build_offload_content("KB komplett auditieren", 120.0))


def test_is_offloaded_handles_none_and_empty():
    assert not is_offloaded(None)
    assert not is_offloaded("")


# ---------------------------------------------------------------------------
# KanbanBoard.offload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_offload_moves_in_progress_to_backlog(board):
    proto = KanbanTask(id=None, chat_id=1, user_id=1, content="lange aufgabe")
    task_id = await board.push_active(proto, agent_id="chat")

    new_content = build_offload_content("lange aufgabe", 300.0)
    assert await board.offload(task_id, new_content)

    task = await board.get(task_id)
    assert task.lane == Lane.BACKLOG
    assert task.content == new_content
    assert task.agent_class is None
    assert task.agent_id is None
    assert task.started_at is None
    assert task.assigned_at is None


@pytest.mark.asyncio
async def test_offload_noop_if_not_in_progress(board):
    task_id = await board.push(KanbanTask(id=None, chat_id=1, user_id=1, content="x"))
    assert not await board.offload(task_id, "neu")
    task = await board.get(task_id)
    assert task.lane == Lane.BACKLOG
    assert task.content == "x"  # unchanged


@pytest.mark.asyncio
async def test_offload_sets_notify(board):
    task_id = await board.push_active(
        KanbanTask(id=None, chat_id=1, user_id=1, content="x"), agent_id="chat"
    )
    board._notify.clear()
    await board.offload(task_id, "neu")
    assert board._notify.is_set()


# ---------------------------------------------------------------------------
# Daemon._offload_after_timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_offloads_task_to_board(board):
    d = _make_daemon(board)
    task_id = await board.push_active(
        KanbanTask(id=None, chat_id=7, user_id=3, content="KB audit"), agent_id="chat"
    )

    await d._offload_after_timeout(7, 3, "KB audit", task_id, 300.0)

    task = await board.get(task_id)
    assert task.lane == Lane.BACKLOG
    assert is_offloaded(task.content)
    assert "KB audit" in task.content
    # exactly one task on the board — no duplicate push
    assert len(await board.list_all()) == 1
    # user got the background notice
    msg = d._api.send_message.call_args[0][1]
    assert "Hintergrund" in msg


@pytest.mark.asyncio
async def test_timeout_pushes_fresh_task_when_no_active_task(board):
    d = _make_daemon(board)

    await d._offload_after_timeout(7, 3, "KB audit", None, 300.0)

    tasks = await board.list_all()
    assert len(tasks) == 1
    assert tasks[0].lane == Lane.BACKLOG
    assert is_offloaded(tasks[0].content)


@pytest.mark.asyncio
async def test_no_reoffload_loop_for_marked_content(board):
    """Already-offloaded content must never go back on the board."""
    d = _make_daemon(board)
    marked = build_offload_content("KB audit", 300.0)
    task_id = await board.push_active(
        KanbanTask(id=None, chat_id=7, user_id=3, content=marked), agent_id="chat"
    )

    await d._offload_after_timeout(7, 3, marked, task_id, 300.0)

    task = await board.get(task_id)
    assert task.lane == Lane.FAILED
    # no second task created
    assert len(await board.list_all()) == 1
    msg = d._api.send_message.call_args[0][1]
    assert "Hintergrund" not in msg


@pytest.mark.asyncio
async def test_timeout_without_board_sends_plain_message():
    d = _make_daemon(None)
    await d._offload_after_timeout(7, 3, "KB audit", None, 300.0)
    assert d._api.send_message.await_count == 1
