import hashlib
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .. import config, db
from ..config_store import save_config as _store_save_config
from ..memory import store as memory_store
from ..auth.users import get_user, set_role
from ..kanban.models import Lane
from ..mode import DaemonMode
from ..logging_setup import log_path

app = FastAPI(title="Comms", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_daemon_ref: Any = None
_setup_token: str | None = None


def set_daemon(daemon: Any) -> None:
    global _daemon_ref
    _daemon_ref = daemon


def set_setup_token(token: str) -> None:
    global _setup_token
    _setup_token = token


def _verify_token(request: Request) -> None:
    cfg = config.section("web")
    raw_token = cfg.get("auth_token", "")
    expected = hashlib.sha256(raw_token.encode()).hexdigest()
    token = request.headers.get("X-Auth-Token") or request.cookies.get("auth")
    if token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _get_conn():
    return await db.get_conn()


@app.get("/health")
async def health():
    if _daemon_ref:
        return _daemon_ref.health()
    return {"status": "ok", "mode": "startup"}


@app.get("/api/setup")
async def get_setup_status():
    mode = _daemon_ref._mode_mgr.mode.value if _daemon_ref else "startup"
    return {"mode": mode, "setup_required": mode == "initialize"}


@app.post("/api/setup/save")
async def save_setup(body: dict, x_setup_token: str | None = Header(default=None)):
    global _setup_token
    if not _daemon_ref or _daemon_ref._mode_mgr.mode != DaemonMode.INITIALIZE:
        raise HTTPException(status_code=409, detail="Setup only available in initialize mode")
    if not _setup_token or x_setup_token != _setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")

    cfg = body.get("config", {})
    tg_token = cfg.get("telegram", {}).get("token", "")
    if not tg_token or tg_token == "YOUR_BOT_TOKEN":
        raise HTTPException(status_code=400, detail="telegram.token required")
    web_token = cfg.get("web", {}).get("auth_token", "")
    if not web_token:
        raise HTTPException(status_code=400, detail="web.auth_token required")
    admin_ids = cfg.get("telegram", {}).get("admin_chat_ids", [])
    if not admin_ids:
        raise HTTPException(status_code=400, detail="telegram.admin_chat_ids required")

    conn = await db.init_config()
    await _store_save_config(conn, cfg)
    await conn.close()

    _setup_token = None  # single-use

    return {"ok": True}


@app.get("/api/status", dependencies=[Depends(_verify_token)])
async def status():
    if _daemon_ref:
        return _daemon_ref.health()
    return {"status": "unknown", "mode": "startup"}


@app.get("/api/mode", dependencies=[Depends(_verify_token)])
async def get_mode():
    if _daemon_ref:
        return _daemon_ref._mode_mgr.as_dict()
    return {"mode": "startup"}


@app.get("/api/usage", dependencies=[Depends(_verify_token)])
async def get_usage():
    if _daemon_ref and _daemon_ref._usage_state is not None:
        return _daemon_ref._usage_state.as_dict()
    return {"tokens_used": None, "tokens_limit": None, "usage_pct": None, "reset_in_seconds": None}


@app.post("/api/repair/trigger", dependencies=[Depends(_verify_token)])
async def trigger_repair(body: dict):
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    error = body.get("error", "Manual repair triggered via web UI")
    await _daemon_ref.trigger_repair(error)
    return {"ok": True, "mode": "repair"}


@app.post("/api/repair/exit", dependencies=[Depends(_verify_token)])
async def exit_repair():
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    await _daemon_ref.exit_repair()
    return {"ok": True, "mode": "run"}


@app.post("/api/repair/chat", dependencies=[Depends(_verify_token)])
async def repair_chat(body: dict):
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    mechanic = _daemon_ref._mechanic
    if not mechanic:
        raise HTTPException(status_code=404, detail="No active mechanic session")
    message = body.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    reply = await mechanic.followup(message)
    return {"reply": reply}


@app.get("/api/repair/report", dependencies=[Depends(_verify_token)])
async def get_repair_report():
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    report = _daemon_ref._mechanic_report
    if report is None:
        return {"report": None}
    return {"report": report}


@app.get("/api/tasks", dependencies=[Depends(_verify_token)])
async def get_tasks(status: str | None = None, limit: int = 50):
    conn = await _get_conn()
    if status:
        async with conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    await conn.close()
    return [dict(r) for r in rows]


@app.get("/api/messages", dependencies=[Depends(_verify_token)])
async def get_messages(chat_id: int | None = None, limit: int = 50):
    conn = await _get_conn()
    if chat_id:
        async with conn.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    else:
        async with conn.execute(
            "SELECT * FROM messages ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
    await conn.close()
    return [dict(r) for r in rows]


@app.get("/api/memory", dependencies=[Depends(_verify_token)])
async def get_memory(user_id: int | None = None, q: str | None = None):
    conn = await _get_conn()
    if q:
        items = await memory_store.search(conn, q, user_id=user_id)
    else:
        items = await memory_store.list_all(conn, user_id=user_id)
    await conn.close()
    return items


@app.get("/api/users", dependencies=[Depends(_verify_token)])
async def get_users():
    conn = await _get_conn()
    async with conn.execute("SELECT * FROM users ORDER BY last_seen DESC") as cur:
        rows = await cur.fetchall()
    await conn.close()
    return [dict(r) for r in rows]


@app.post("/api/users/{telegram_id}/role", dependencies=[Depends(_verify_token)])
async def update_user_role(telegram_id: int, body: dict):
    role = body.get("role")
    if role not in ("admin", "user", "blocked"):
        raise HTTPException(status_code=400, detail="Invalid role")
    conn = await _get_conn()
    await set_role(conn, telegram_id, role)
    await conn.close()
    return {"ok": True}


@app.get("/api/approvals", dependencies=[Depends(_verify_token)])
async def get_approvals():
    if _daemon_ref:
        return _daemon_ref._security.pending_list()
    return []


@app.post("/api/approvals/{approval_id}/approve", dependencies=[Depends(_verify_token)])
async def approve_action(approval_id: int):
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    if not _daemon_ref._security.approve(approval_id, admin_id=0):
        raise HTTPException(status_code=404, detail="No pending approval with that ID")
    return {"ok": True}


@app.post("/api/approvals/{approval_id}/deny", dependencies=[Depends(_verify_token)])
async def deny_action(approval_id: int):
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    if not _daemon_ref._security.deny(approval_id, admin_id=0):
        raise HTTPException(status_code=404, detail="No pending approval with that ID")
    return {"ok": True}


@app.get("/api/kanban", dependencies=[Depends(_verify_token)])
async def get_kanban(lane: str | None = None, limit: int = 100):
    conn = await _get_conn()
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


@app.get("/api/kanban/counts", dependencies=[Depends(_verify_token)])
async def get_kanban_counts():
    conn = await _get_conn()
    async with conn.execute(
        "SELECT lane, COUNT(*) as n FROM kanban_tasks GROUP BY lane"
    ) as cur:
        rows = await cur.fetchall()
    await conn.close()
    return {r["lane"]: r["n"] for r in rows}


@app.post("/api/config/reload", dependencies=[Depends(_verify_token)])
async def reload_config():
    try:
        config.reload()
        return {"ok": True, "message": "Config reloaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tokens", dependencies=[Depends(_verify_token)])
async def get_tokens(period: str = "24h"):
    periods = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}
    seconds = periods.get(period, 86400)
    import time
    since = int(time.time()) - seconds

    conn = await _get_conn()
    async with conn.execute(
        """SELECT agent_class,
                  SUM(input_tokens) as input_total,
                  SUM(output_tokens) as output_total,
                  SUM(cache_read_tokens) as cache_read_total,
                  SUM(cache_write_tokens) as cache_write_total,
                  SUM(cost_usd) as cost_total,
                  COUNT(*) as calls
           FROM token_usage WHERE timestamp >= ?
           GROUP BY agent_class""",
        (since,),
    ) as cur:
        stat_rows = await cur.fetchall()

    bucket_seconds = 3600 if seconds <= 86400 else 21600
    async with conn.execute(
        """SELECT (timestamp / ?) * ? as bucket,
                  agent_class,
                  SUM(input_tokens + output_tokens) as total_tokens,
                  SUM(cost_usd) as total_cost
           FROM token_usage WHERE timestamp >= ?
           GROUP BY bucket, agent_class
           ORDER BY bucket ASC""",
        (bucket_seconds, bucket_seconds, since),
    ) as cur:
        ts_rows = await cur.fetchall()

    await conn.close()

    stats = {
        r["agent_class"]: {
            "input": r["input_total"] or 0,
            "output": r["output_total"] or 0,
            "cache_read": r["cache_read_total"] or 0,
            "cache_write": r["cache_write_total"] or 0,
            "cost_usd": round(r["cost_total"] or 0.0, 6),
            "calls": r["calls"] or 0,
        }
        for r in stat_rows
    }
    total_cost = sum(v["cost_usd"] for v in stats.values())
    timeseries = [
        {
            "bucket": r["bucket"],
            "agent_class": r["agent_class"],
            "tokens": r["total_tokens"],
            "cost_usd": round(r["total_cost"] or 0.0, 6),
        }
        for r in ts_rows
    ]
    return {"period": period, "stats": stats, "total_cost_usd": round(total_cost, 6), "timeseries": timeseries}


@app.get("/api/logs", dependencies=[Depends(_verify_token)])
async def get_logs(lines: int = 200):
    cfg = config.section("logging")
    path = log_path(cfg.get("dir", "/data/logs"))
    if not path.exists():
        return PlainTextResponse("")
    with open(path, encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    return PlainTextResponse("".join(all_lines[-lines:]))


@app.get("/", response_class=HTMLResponse)
async def index():
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path) as f:
            return f.read()
    return "<h1>Comms</h1><p>UI not built yet.</p>"
