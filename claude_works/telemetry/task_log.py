"""Per-task structured logging — writes to task_logs table and in-memory buffer."""
import asyncio
import time
from collections import defaultdict

from .. import db as _db

_buffers: dict[int, list[dict]] = defaultdict(list)
_MAX_BUFFER = 500


def _append(task_id: int, level: str, msg: str) -> None:
    entry = {"ts": int(time.time()), "level": level, "msg": msg}
    buf = _buffers[task_id]
    buf.append(entry)
    if len(buf) > _MAX_BUFFER:
        buf.pop(0)


def get_buffer(task_id: int) -> list[dict]:
    return list(_buffers.get(task_id, []))


def clear_buffer(task_id: int) -> None:
    _buffers.pop(task_id, None)


async def _write(task_id: int, level: str, msg: str) -> None:
    try:
        conn = await _db.get_conn()
        await conn.execute(
            "INSERT INTO task_logs (task_id, ts, level, msg) VALUES (?, ?, ?, ?)",
            (task_id, int(time.time()), level, msg),
        )
        await conn.commit()
        await conn.close()
    except Exception:
        pass


def log(task_id: int, level: str, msg: str) -> None:
    _append(task_id, level, msg)
    asyncio.ensure_future(_write(task_id, level, msg))


def info(task_id: int, msg: str) -> None:
    log(task_id, "info", msg)


def warn(task_id: int, msg: str) -> None:
    log(task_id, "warn", msg)


def error(task_id: int, msg: str) -> None:
    log(task_id, "error", msg)


class TaskLogger:
    """Convenience wrapper that binds task_id."""

    def __init__(self, task_id: int) -> None:
        self._task_id = task_id

    def info(self, msg: str) -> None:
        log(self._task_id, "info", msg)

    def warn(self, msg: str) -> None:
        log(self._task_id, "warn", msg)

    def error(self, msg: str) -> None:
        log(self._task_id, "error", msg)
