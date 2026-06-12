from fastapi import APIRouter, Depends, HTTPException

from ...kanban.models import Lane
from .. import state
from ..deps import get_conn, verify_token

router = APIRouter()


@router.get("/api/kanban", dependencies=[Depends(verify_token)])
async def get_kanban(lane: str | None = None, limit: int = 100):
    conn = await get_conn()
    if lane:
        try:
            lane_enum = Lane(lane)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid lane: {lane}")
        async with conn.execute(
            "SELECT * FROM kanban_tasks WHERE lane = ? ORDER BY created_at DESC LIMIT ?",
            (lane_enum.value, limit),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with conn.execute(
            "SELECT * FROM kanban_tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    await conn.close()
    return [dict(r) for r in rows]


@router.get("/api/kanban/counts", dependencies=[Depends(verify_token)])
async def get_kanban_counts():
    conn = await get_conn()
    async with conn.execute(
        "SELECT lane, COUNT(*) as n FROM kanban_tasks GROUP BY lane"
    ) as cur:
        rows = await cur.fetchall()
    await conn.close()
    return {r["lane"]: r["n"] for r in rows}


@router.delete("/api/kanban", dependencies=[Depends(verify_token)])
async def clear_kanban(lane: str | None = None, clear_all: bool = False):
    conn = await get_conn()
    if clear_all:
        async with conn.execute("DELETE FROM kanban_tasks") as cur:
            deleted = cur.rowcount
    elif lane:
        try:
            lane_enum = Lane(lane)
        except ValueError:
            await conn.close()
            raise HTTPException(status_code=400, detail=f"Invalid lane: {lane}")
        async with conn.execute("DELETE FROM kanban_tasks WHERE lane = ?", (lane_enum.value,)) as cur:
            deleted = cur.rowcount
    else:
        async with conn.execute(
            "DELETE FROM kanban_tasks WHERE lane IN (?, ?)",
            (Lane.DONE.value, Lane.FAILED.value),
        ) as cur:
            deleted = cur.rowcount
    await conn.commit()
    await conn.close()
    return {"deleted": deleted}


@router.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(verify_token)])
async def cancel_task(task_id: int):
    conn = await get_conn()
    async with conn.execute("SELECT lane FROM kanban_tasks WHERE id = ?", (task_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        await conn.close()
        raise HTTPException(status_code=404, detail="Task not found")
    if row["lane"] in ("done", "failed"):
        await conn.close()
        raise HTTPException(status_code=400, detail="Task already finished")
    if state.daemon_ref and state.daemon_ref._coordinator:
        state.daemon_ref._coordinator.cancel_task(task_id)
    await conn.execute(
        "UPDATE kanban_tasks SET lane = 'failed', error = 'Abgebrochen', completed_at = ? WHERE id = ?",
        (int(__import__("time").time()), task_id),
    )
    await conn.commit()
    await conn.close()
    return {"ok": True}


@router.get("/api/tasks/{task_id}/logs", dependencies=[Depends(verify_token)])
async def get_task_logs(task_id: int, since: int = 0):
    from ...telemetry.task_log import get_buffer
    buf = get_buffer(task_id)
    if buf:
        entries = [e for e in buf if e["ts"] > since]
        return {"task_id": task_id, "logs": entries, "source": "memory"}
    conn = await get_conn()
    async with conn.execute(
        "SELECT ts, level, msg FROM task_logs WHERE task_id = ? AND ts > ? ORDER BY ts ASC",
        (task_id, since),
    ) as cur:
        rows = await cur.fetchall()
    await conn.close()
    return {"task_id": task_id, "logs": [{"ts": r[0], "level": r[1], "msg": r[2]} for r in rows], "source": "db"}
