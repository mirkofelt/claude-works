from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from ... import config, db
from ...config_store import save_config as _store_save_config
from ...logging_setup import log_path
from .. import state
from ..deps import get_conn, verify_token

router = APIRouter()


@router.get("/health")
async def health_check():
    from fastapi.responses import JSONResponse
    d = state.daemon_ref
    if d is None or d._conn is None or not d._running:
        return JSONResponse({"status": "degraded"}, status_code=503)
    return {"status": "ok"}


@router.get("/api/status", dependencies=[Depends(verify_token)])
async def status():
    if state.daemon_ref:
        return state.daemon_ref.health()
    return {"status": "unknown", "mode": "startup"}


@router.get("/api/mode", dependencies=[Depends(verify_token)])
async def get_mode():
    if state.daemon_ref:
        return state.daemon_ref._mode_mgr.as_dict()
    return {"mode": "startup"}


@router.post("/api/mode", dependencies=[Depends(verify_token)])
async def set_mode(body: dict):
    mode = str(body.get("mode", "run")).lower()
    if mode not in ("run", "repair"):
        raise HTTPException(status_code=400, detail="mode must be 'run' or 'repair'")
    cfg = config.get()
    cfg.setdefault("system", {})["mode"] = mode
    conn = await db.init_config()
    await _store_save_config(conn, cfg)
    await conn.close()
    config._settings = cfg
    return {"mode": mode}


@router.get("/api/usage", dependencies=[Depends(verify_token)])
async def get_usage():
    if state.daemon_ref and state.daemon_ref._usage_state is not None:
        return state.daemon_ref._usage_state.as_dict()
    return {
        "tokens_used": None, "tokens_limit": None, "usage_pct": None, "reset_in_seconds": None,
        "session_pct": None, "weekly_all_pct": None, "weekly_models": [],
        "session_reset_at": None, "weekly_reset_at": None,
    }


@router.post("/api/repair/trigger", dependencies=[Depends(verify_token)])
async def trigger_repair(body: dict):
    if not state.daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    error = body.get("error", "Manual repair triggered via web UI")
    await state.daemon_ref.trigger_repair(error)
    return {"ok": True, "mode": "repair"}


@router.post("/api/repair/exit", dependencies=[Depends(verify_token)])
async def exit_repair():
    if not state.daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    await state.daemon_ref.exit_repair()
    return {"ok": True, "mode": "run"}


@router.post("/api/repair/chat", dependencies=[Depends(verify_token)])
async def repair_chat(body: dict):
    if not state.daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    mechanic = state.daemon_ref._mechanic
    if not mechanic:
        raise HTTPException(status_code=404, detail="No active mechanic session")
    message = body.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    reply = await mechanic.followup(message)
    return {"reply": reply}


@router.get("/api/repair/report", dependencies=[Depends(verify_token)])
async def get_repair_report():
    if not state.daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    report = state.daemon_ref._mechanic_report
    if report is None:
        return {"report": None}
    return {"report": report}


@router.get("/api/admin/chat/history", dependencies=[Depends(verify_token)])
async def admin_chat_history():
    if not state.daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    messages = await state.daemon_ref.web_admin_history()
    return {"messages": messages}


@router.post("/api/admin/chat", dependencies=[Depends(verify_token)])
async def admin_chat(body: dict):
    if not state.daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    result = await state.daemon_ref.web_admin_chat(message)
    return result


@router.get("/api/deploy/status", dependencies=[Depends(verify_token)])
async def get_deploy_status():
    import httpx
    cfg = config.get()
    sys_cfg = cfg.get("system", {})
    dev_mode = sys_cfg.get("dev_mode", False)
    dg = sys_cfg.get("claude_guard", {})
    guard_url = dg.get("url", "")
    guard_reachable = False
    token = dg.get("token", "")
    if guard_url and dev_mode and token:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{guard_url}/health?token={token}")
                guard_reachable = r.status_code == 200
        except Exception:
            pass
    return {"dev_mode": dev_mode, "guard_url": guard_url, "guard_reachable": guard_reachable}


@router.post("/api/deploy/trigger", dependencies=[Depends(verify_token)])
async def trigger_deploy():
    import httpx
    cfg = config.get()
    sys_cfg = cfg.get("system", {})
    if not sys_cfg.get("dev_mode", False):
        raise HTTPException(status_code=403, detail="dev_mode is disabled")
    dg = sys_cfg.get("claude_guard", {})
    guard_url = dg.get("url", "").rstrip("/")
    token = dg.get("token", "")
    if not guard_url or not token:
        raise HTTPException(status_code=400, detail="claude_guard.url and .token not configured")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{guard_url}/deploy?token={token}")
            return {"status": "ok" if r.status_code == 200 else "error", "detail": r.text}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/api/deploy/rollback", dependencies=[Depends(verify_token)])
async def trigger_rollback():
    import httpx
    cfg = config.get()
    sys_cfg = cfg.get("system", {})
    if not sys_cfg.get("dev_mode", False):
        raise HTTPException(status_code=403, detail="dev_mode is disabled")
    dg = sys_cfg.get("claude_guard", {})
    guard_url = dg.get("url", "").rstrip("/")
    token = dg.get("token", "")
    if not guard_url or not token:
        raise HTTPException(status_code=400, detail="claude_guard not configured")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{guard_url}/rollback?token={token}")
            return {"status": "ok" if r.status_code == 200 else "error", "detail": r.text}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/api/cron", dependencies=[Depends(verify_token)])
async def get_cron_status():
    if not state.daemon_ref or not getattr(state.daemon_ref, "_cron", None):
        return {"jobs": []}
    return {"jobs": await state.daemon_ref._cron.status()}


@router.get("/api/logs", dependencies=[Depends(verify_token)])
async def get_logs(lines: int = 200):
    cfg = config.section("logging")
    path = log_path(cfg.get("dir", "/data/logs"))
    if not path.exists():
        return PlainTextResponse("")
    with open(path, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return PlainTextResponse("".join(all_lines[-lines:]))
