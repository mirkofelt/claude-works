import asyncio
import hashlib
import hmac
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Depends, Header, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .. import config, db
from ..config_store import save_config as _store_save_config
from ..knowledge import store as knowledge_store
from ..auth.users import get_user, set_role
from ..kanban.models import Lane
from ..mode import DaemonMode
from ..logging_setup import log_path

app = FastAPI(title="Claude Works", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # no cross-origin access; UI is served same-origin
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-Auth-Token", "Content-Type"],
)

_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

_daemon_ref: Any = None
_setup_token: str | None = None
_cli_auth_proc: asyncio.subprocess.Process | None = None
_runtime_cli_auth_proc: asyncio.subprocess.Process | None = None


# ---------------------------------------------------------------------------
# Rate limiting (in-process sliding window, no external dep)
# ---------------------------------------------------------------------------

class _SlidingWindow:
    """Thread-safe sliding window counter per key (typically client IP)."""

    def __init__(self, limit: int, window: int) -> None:
        self._limit = limit
        self._window = window
        self._log: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Record a hit. Returns True if within limit, False if exceeded."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            log = self._log[key]
            pruned = [t for t in log if t > cutoff]
            if len(pruned) >= self._limit:
                self._log[key] = pruned
                return False
            pruned.append(now)
            self._log[key] = pruned
            return True


# 120 API requests / 60 s per IP (general DoS protection)
_api_limiter = _SlidingWindow(limit=120, window=60)
# 10 failed auth attempts / 300 s per IP (brute-force lockout)
_auth_fail_limiter = _SlidingWindow(limit=10, window=300)


def _client_ip(request: Request) -> str:
    """Return real client IP, honouring Cloudflare / reverse-proxy headers when trusted_proxy is set."""
    cfg = config.section("web") if config._settings else {}
    if cfg.get("trusted_proxy"):
        cf = request.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip()
        fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if fwd:
            return fwd
    return request.client.host if request.client else "unknown"


def _is_https(request: Request) -> bool:
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    return proto == "https"


def set_daemon(daemon: Any) -> None:
    global _daemon_ref
    _daemon_ref = daemon


def set_setup_token(token: str) -> None:
    global _setup_token
    _setup_token = token


def _verify_token(request: Request) -> None:
    ip = _client_ip(request)
    if not _api_limiter.hit(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down")
    cfg = config.section("web")
    raw_token = cfg.get("auth_token", "")
    if not raw_token:
        raise HTTPException(status_code=503, detail="Auth not configured")
    expected = hashlib.sha256(raw_token.encode()).hexdigest()
    token = request.headers.get("X-Auth-Token") or request.cookies.get("auth") or ""
    if not hmac.compare_digest(token, expected):
        if not _auth_fail_limiter.hit(ip):
            raise HTTPException(status_code=429, detail="Too many failed auth attempts — locked out for 5 minutes")
        raise HTTPException(status_code=401, detail="Unauthorized")


async def _get_conn():
    return await db.get_conn()


@app.post("/api/auth")
async def login(request: Request, response: Response):
    """Exchange raw auth token for a session cookie with correct security flags."""
    ip = _client_ip(request)
    if not _api_limiter.hit(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    body = await request.json()
    token = body.get("token", "")
    cfg = config.section("web") if config._settings else {}
    raw_token = cfg.get("auth_token", "")
    if not raw_token:
        raise HTTPException(status_code=503, detail="Auth not configured")
    expected = hashlib.sha256(raw_token.encode()).hexdigest()
    candidate = hashlib.sha256(token.encode()).hexdigest()
    if not hmac.compare_digest(candidate, expected):
        if not _auth_fail_limiter.hit(ip):
            raise HTTPException(status_code=429, detail="Too many failed attempts — locked out for 5 minutes")
        raise HTTPException(status_code=401, detail="Unauthorized")
    secure = _is_https(request)
    response.set_cookie(
        key="auth",
        value=expected,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=86400 * 30,
    )
    return {"ok": True}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(key="auth", samesite="strict")
    return {"ok": True}


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


@app.post("/api/setup/cli-auth/start")
async def cli_auth_start(body: dict, x_setup_token: str | None = Header(default=None)):
    global _cli_auth_proc, _setup_token
    if not _daemon_ref or _daemon_ref._mode_mgr.mode != DaemonMode.INITIALIZE:
        raise HTTPException(status_code=409, detail="Setup only available in initialize mode")
    if not _setup_token or x_setup_token != _setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")

    binary = body.get("cli_binary", "claude").strip()
    if not binary or not re.match(r'^[a-zA-Z0-9_./-]+$', binary):
        raise HTTPException(status_code=400, detail="Invalid cli_binary path")

    if _cli_auth_proc and _cli_auth_proc.returncode is None:
        try:
            _cli_auth_proc.kill()
        except Exception:
            pass

    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "auth", "login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"CLI binary not found: {binary}")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to start auth: {exc}")

    _cli_auth_proc = proc

    url = None
    buf = ""
    try:
        deadline = asyncio.get_event_loop().time() + 20.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                break
            buf += chunk.decode(errors="replace")
            m = re.search(r'https://[^\s]+', buf)
            if m:
                url = m.group().rstrip('.')
                break
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Auth start error: {exc}")

    if not url:
        raise HTTPException(status_code=500, detail=f"No auth URL found. Output: {buf[:400]}")

    return {"url": url}


@app.post("/api/setup/cli-auth/confirm")
async def cli_auth_confirm(body: dict, x_setup_token: str | None = Header(default=None)):
    global _cli_auth_proc, _setup_token
    if not _daemon_ref or _daemon_ref._mode_mgr.mode != DaemonMode.INITIALIZE:
        raise HTTPException(status_code=409, detail="Setup only available in initialize mode")
    if not _setup_token or x_setup_token != _setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")
    if not _cli_auth_proc or _cli_auth_proc.returncode is not None:
        raise HTTPException(status_code=409, detail="No active auth session — call /start first")

    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code required")

    proc = _cli_auth_proc
    try:
        proc.stdin.write((code + "\n").encode())
        await proc.stdin.drain()
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        returncode = proc.returncode
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise HTTPException(status_code=504, detail="Auth confirmation timed out")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Auth confirm error: {exc}")
    finally:
        _cli_auth_proc = None

    if returncode != 0:
        out = stdout.decode(errors="replace") if stdout else ""
        raise HTTPException(status_code=500, detail=f"Auth failed (exit {returncode}): {out[:300]}")

    return {"ok": True}


@app.post("/api/cli-auth/start", dependencies=[Depends(_verify_token)])
async def runtime_cli_auth_start():
    """Start claude auth login — available in any mode."""
    global _runtime_cli_auth_proc
    cfg = config.section("llm") if config._settings else {}
    binary = cfg.get("cli_binary") or "claude"
    if not re.match(r'^[a-zA-Z0-9_./-]+$', binary):
        raise HTTPException(status_code=400, detail="Invalid cli_binary")
    if _runtime_cli_auth_proc and _runtime_cli_auth_proc.returncode is None:
        try:
            _runtime_cli_auth_proc.kill()
        except Exception:
            pass
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "auth", "login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"CLI binary not found: {binary}")
    _runtime_cli_auth_proc = proc
    url = None
    buf = ""
    try:
        deadline = asyncio.get_event_loop().time() + 20.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                break
            buf += chunk.decode(errors="replace")
            m = re.search(r'https://[^\s]+', buf)
            if m:
                url = m.group().rstrip('.')
                break
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Auth start error: {exc}")
    if not url:
        raise HTTPException(status_code=500, detail=f"No auth URL found. Output: {buf[:400]}")
    return {"url": url}


@app.post("/api/cli-auth/confirm", dependencies=[Depends(_verify_token)])
async def runtime_cli_auth_confirm(body: dict):
    """Submit auth code — available in any mode."""
    global _runtime_cli_auth_proc
    if not _runtime_cli_auth_proc or _runtime_cli_auth_proc.returncode is not None:
        raise HTTPException(status_code=409, detail="No active auth session — call /start first")
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code required")
    proc = _runtime_cli_auth_proc
    try:
        proc.stdin.write((code + "\n").encode())
        await proc.stdin.drain()
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        returncode = proc.returncode
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise HTTPException(status_code=504, detail="Auth confirmation timed out")
    finally:
        _runtime_cli_auth_proc = None
    if returncode != 0:
        out = stdout.decode(errors="replace") if stdout else ""
        raise HTTPException(status_code=500, detail=f"Auth failed (exit {returncode}): {out[:300]}")
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


@app.get("/api/admin/chat/history", dependencies=[Depends(_verify_token)])
async def admin_chat_history():
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    messages = await _daemon_ref.web_admin_history()
    return {"messages": messages}


@app.post("/api/admin/chat", dependencies=[Depends(_verify_token)])
async def admin_chat(body: dict):
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    reply = await _daemon_ref.web_admin_chat(message)
    return {"reply": reply}


@app.get("/api/repair/report", dependencies=[Depends(_verify_token)])
async def get_repair_report():
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    report = _daemon_ref._mechanic_report
    if report is None:
        return {"report": None}
    return {"report": report}


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


@app.get("/api/knowledge", dependencies=[Depends(_verify_token)])
async def get_knowledge(q: str | None = None, type: str | None = None, page: int = 1, page_size: int = 25):
    page_size = max(1, min(page_size, 200))
    page = max(1, page)
    conn = await _get_conn()
    if q:
        items = await knowledge_store.search(conn, q, limit=page_size)
        total = len(items)
    else:
        total = await knowledge_store.count(conn, type=type)
        offset = (page - 1) * page_size
        items = await knowledge_store.list_all(conn, type=type, limit=page_size, offset=offset)
    await conn.close()
    pages = max(1, (total + page_size - 1) // page_size)
    return {"items": items, "total": total, "page": page, "page_size": page_size, "pages": pages}


@app.post("/api/knowledge", dependencies=[Depends(_verify_token)])
async def add_knowledge(body: dict):
    title = body.get("title", "").strip()
    content = body.get("content", "").strip()
    if not title or not content:
        raise HTTPException(status_code=400, detail="title and content required")
    conn = await _get_conn()
    entry_id = await knowledge_store.add(
        conn,
        title=title,
        content=content,
        type=body.get("type", "note"),
        tags=body.get("tags"),
        source="admin",
    )
    await conn.close()
    return {"id": entry_id}


@app.put("/api/knowledge/{entry_id}", dependencies=[Depends(_verify_token)])
async def update_knowledge(entry_id: int, body: dict):
    conn = await _get_conn()
    raw_tags = body.get("tags")
    tags = raw_tags if isinstance(raw_tags, list) else (
        [t.strip() for t in raw_tags.split(",") if t.strip()] if isinstance(raw_tags, str) else None
    )
    ok = await knowledge_store.update(
        conn,
        entry_id,
        title=body.get("title") or None,
        content=body.get("content") or None,
        type=body.get("type") or None,
        tags=tags,
    )
    await conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"ok": True}


@app.delete("/api/knowledge/{entry_id}", dependencies=[Depends(_verify_token)])
async def delete_knowledge(entry_id: int):
    conn = await _get_conn()
    ok = await knowledge_store.delete(conn, entry_id)
    await conn.close()
    if not ok:
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"ok": True}


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


@app.post("/api/users", dependencies=[Depends(_verify_token)])
async def add_user(body: dict):
    from ..auth.users import upsert_user
    telegram_id = body.get("telegram_id")
    name = body.get("name") or None
    role = body.get("role", "user")
    if not isinstance(telegram_id, int) or telegram_id <= 0:
        raise HTTPException(status_code=400, detail="telegram_id required (positive int)")
    if role not in ("admin", "user", "blocked"):
        raise HTTPException(status_code=400, detail="Invalid role")
    conn = await _get_conn()
    await upsert_user(conn, telegram_id, name)
    await set_role(conn, telegram_id, role)
    await conn.close()
    return {"ok": True}


@app.put("/api/users/{telegram_id}", dependencies=[Depends(_verify_token)])
async def update_user(telegram_id: int, body: dict):
    conn = await _get_conn()
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
    await conn.close()
    return {"ok": True}


@app.delete("/api/users/{telegram_id}", dependencies=[Depends(_verify_token)])
async def delete_user(telegram_id: int):
    conn = await _get_conn()
    async with conn.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,)) as cur:
        deleted = cur.rowcount
    await conn.commit()
    await conn.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
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


@app.get("/api/security/allowlist", dependencies=[Depends(_verify_token)])
async def get_allowlist():
    if not _daemon_ref:
        return {"always_allowed_actions": [], "skip_all": False}
    sec = _daemon_ref._security
    return {
        "always_allowed_actions": sec.always_allowed_actions,
        "skip_all": sec.skip_all,
    }


@app.delete("/api/security/allowlist/{action_type}", dependencies=[Depends(_verify_token)])
async def remove_from_allowlist(action_type: str):
    if not _daemon_ref:
        raise HTTPException(status_code=503, detail="Daemon not running")
    sec = _daemon_ref._security
    sec._always_allowed_actions.discard(action_type)
    if action_type == "__all__":
        sec._skip_all = False
    sec._save_allowlist()
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


@app.delete("/api/kanban", dependencies=[Depends(_verify_token)])
async def clear_kanban(lane: str | None = None, clear_all: bool = False):
    conn = await _get_conn()
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


@app.get("/api/config", dependencies=[Depends(_verify_token)])
async def get_config():
    cfg = config.get()
    return {"config": cfg}


@app.post("/api/config/save", dependencies=[Depends(_verify_token)])
async def save_config_endpoint(body: dict):
    cfg = body.get("config")
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="config must be a JSON object")
    try:
        conn = await db.init_config()
        await _store_save_config(conn, cfg)
        await conn.close()
        config.set(cfg)
        return {"ok": True, "message": "Config saved and applied"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config/reload", dependencies=[Depends(_verify_token)])
async def reload_config():
    from ..config_store import load_config as _load_db_cfg
    try:
        conn = await db.init_config()
        cfg = await _load_db_cfg(conn)
        row = None
        if cfg:
            async with conn.execute(
                "SELECT updated_at FROM daemon_config WHERE id=1"
            ) as cur:
                row = await cur.fetchone()
        await conn.close()
        if not cfg:
            raise HTTPException(status_code=404, detail="No config in DB")
        config.set(cfg)
        if row:
            config._config_updated_at = row["updated_at"]
        return {"ok": True, "message": "Config reloaded from DB"}
    except HTTPException:
        raise
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

    cli_usage_ts = []
    llm_cfg = config.section("llm")
    if llm_cfg.get("provider") == "cli":
        async with conn.execute(
            "SELECT sampled_at, tokens_used, tokens_limit FROM usage_snapshots WHERE sampled_at >= ? ORDER BY sampled_at ASC",
            (since,),
        ) as cur:
            cli_rows = await cur.fetchall()
        cli_usage_ts = [
            {"sampled_at": r["sampled_at"], "tokens_used": r["tokens_used"], "tokens_limit": r["tokens_limit"]}
            for r in cli_rows
        ]

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
    return {
        "period": period,
        "stats": stats,
        "total_cost_usd": round(total_cost, 6),
        "timeseries": timeseries,
        "cli_usage": cli_usage_ts,
    }


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
    return "<h1>Claude Works</h1><p>UI not built yet.</p>"
