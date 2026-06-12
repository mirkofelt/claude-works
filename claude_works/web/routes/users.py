import json as _json

from fastapi import APIRouter, Depends, HTTPException

from ... import db
from ...auth.users import get_user, set_role
from .. import state
from ..deps import get_conn, verify_token

router = APIRouter()


@router.get("/api/users", dependencies=[Depends(verify_token)])
async def get_users():
    conn = await get_conn()
    async with conn.execute("SELECT * FROM users ORDER BY last_seen DESC") as cur:
        rows = await cur.fetchall()
    await conn.close()
    return [dict(r) for r in rows]


@router.post("/api/users/{telegram_id}/role", dependencies=[Depends(verify_token)])
async def update_user_role(telegram_id: int, body: dict):
    role = body.get("role")
    if role not in ("admin", "user", "blocked"):
        raise HTTPException(status_code=400, detail="Invalid role")
    conn = await get_conn()
    await set_role(conn, telegram_id, role)
    await conn.close()
    return {"ok": True}


@router.post("/api/users", dependencies=[Depends(verify_token)])
async def add_user(body: dict):
    from ...auth.users import upsert_user
    telegram_id = body.get("telegram_id")
    name = body.get("name") or None
    role = body.get("role", "user")
    if not isinstance(telegram_id, int) or telegram_id <= 0:
        raise HTTPException(status_code=400, detail="telegram_id required (positive int)")
    if role not in ("admin", "user", "blocked"):
        raise HTTPException(status_code=400, detail="Invalid role")
    conn = await get_conn()
    await upsert_user(conn, telegram_id, name)
    await set_role(conn, telegram_id, role)
    await conn.close()
    return {"ok": True}


@router.put("/api/users/{telegram_id}", dependencies=[Depends(verify_token)])
async def update_user(telegram_id: int, body: dict):
    conn = await get_conn()
    name = body.get("name")
    role = body.get("role")
    if name is not None:
        await conn.execute("UPDATE users SET name = ? WHERE telegram_id = ?", (name, telegram_id))
        await conn.commit()
    if role is not None:
        if role not in ("admin", "user", "blocked"):
            await conn.close()
            raise HTTPException(status_code=400, detail="Invalid role")
        await set_role(conn, telegram_id, role)
    trust_level = body.get("trust_level")
    if trust_level is not None:
        if trust_level not in (0, 1, 2, 3):
            await conn.close()
            raise HTTPException(status_code=400, detail="Invalid trust_level (0-3)")
        from ...auth.users import set_trust
        await set_trust(conn, telegram_id, trust_level)
    if "persona" in body:
        persona = body["persona"] or None
        await conn.execute("UPDATE users SET persona = ? WHERE telegram_id = ?", (persona, telegram_id))
        await conn.commit()
        if state.daemon_ref:
            if persona:
                state.daemon_ref._user_personas[telegram_id] = persona
            else:
                state.daemon_ref._user_personas.pop(telegram_id, None)
            state.daemon_ref._chat_agents.pop(telegram_id, None)
    await conn.close()
    return {"ok": True}


@router.delete("/api/users/{telegram_id}", dependencies=[Depends(verify_token)])
async def delete_user(telegram_id: int):
    conn = await get_conn()
    async with conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
        deleted = cur.rowcount
    await conn.commit()
    await conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@router.get("/api/approvals", dependencies=[Depends(verify_token)])
async def get_approvals():
    if state.daemon_ref:
        return state.daemon_ref._security.pending_list()
    return []


@router.get("/api/approvals/history", dependencies=[Depends(verify_token)])
async def get_approval_history(limit: int = 100):
    conn = await db.init()
    async with conn.execute(
        "SELECT id, action_types, content_preview, task_id, chat_id, decision, decided_by, requested_at, decided_at"
        " FROM approval_log ORDER BY decided_at DESC LIMIT ?",
        (min(limit, 500),),
    ) as cur:
        rows = await cur.fetchall()
    await conn.close()
    return [
        {
            "id": r["id"],
            "action_types": _json.loads(r["action_types"]) if r["action_types"] else [],
            "content_preview": r["content_preview"],
            "task_id": r["task_id"],
            "chat_id": r["chat_id"],
            "decision": r["decision"],
            "decided_by": r["decided_by"],
            "requested_at": r["requested_at"],
            "decided_at": r["decided_at"],
        }
        for r in rows
    ]


@router.post("/api/approvals/{approval_id}/approve", dependencies=[Depends(verify_token)])
async def approve_action(approval_id: int):
    if not state.daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    if not state.daemon_ref._security.approve(approval_id, admin_id=0):
        raise HTTPException(status_code=404, detail="No pending approval with that ID")
    return {"ok": True}


@router.post("/api/approvals/{approval_id}/deny", dependencies=[Depends(verify_token)])
async def deny_action(approval_id: int):
    if not state.daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    if not state.daemon_ref._security.deny(approval_id, admin_id=0):
        raise HTTPException(status_code=404, detail="No pending approval with that ID")
    return {"ok": True}


@router.get("/api/security/allowlist", dependencies=[Depends(verify_token)])
async def get_allowlist():
    if not state.daemon_ref:
        return {"always_allowed_actions": [], "skip_all": False}
    sec = state.daemon_ref._security
    return {
        "always_allowed_actions": sec.always_allowed_actions,
        "skip_all": sec.skip_all,
    }


@router.delete("/api/security/allowlist/{action_type}", dependencies=[Depends(verify_token)])
async def remove_from_allowlist(action_type: str):
    if not state.daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    sec = state.daemon_ref._security
    sec._always_allowed_actions.discard(action_type)
    if action_type == "__all__":
        sec._skip_all = False
    sec._save_allowlist()
    return {"ok": True}
