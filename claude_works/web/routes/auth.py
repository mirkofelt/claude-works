import asyncio
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from ... import config, db
from ...config_store import save_config as _store_save_config
from ...mode import DaemonMode
from .. import state
from ..deps import client_ip, is_https, verify_token

router = APIRouter()


@router.post("/api/auth")
async def login(request: Request, response: Response):
    """Exchange raw auth token for a session cookie with correct security flags."""
    ip = client_ip(request)
    if not state.api_limiter.hit(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    body = await request.json()
    token = body.get("token", "")
    import hashlib
    import hmac
    cfg = config.section("web") if config._settings else {}
    raw_token = cfg.get("auth_token", "")
    if not raw_token:
        raise HTTPException(status_code=503, detail="Auth not configured")
    expected = hashlib.sha256(raw_token.encode()).hexdigest()
    candidate = hashlib.sha256(token.encode()).hexdigest()
    if not hmac.compare_digest(candidate, expected):
        if not state.auth_fail_limiter.hit(ip):
            raise HTTPException(status_code=429, detail="Too many failed attempts — locked out for 5 minutes")
        raise HTTPException(status_code=401, detail="Unauthorized")
    secure = is_https(request)
    response.set_cookie(
        key="auth",
        value=expected,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=86400 * 30,
    )
    return {"ok": True}


@router.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(key="auth", samesite="strict")
    return {"ok": True}


@router.get("/api/setup")
async def get_setup_status():
    mode = state.daemon_ref._mode_mgr.mode.value if state.daemon_ref else "startup"
    return {"mode": mode, "setup_required": mode == "initialize"}


@router.post("/api/setup/save")
async def save_setup(body: dict, x_setup_token: str | None = Header(default=None)):
    if not state.daemon_ref or state.daemon_ref._mode_mgr.mode != DaemonMode.INITIALIZE:
        raise HTTPException(status_code=409, detail="Setup only available in initialize mode")
    if not state.setup_token or x_setup_token != state.setup_token:
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

    state.setup_token = None  # single-use

    return {"ok": True}


@router.post("/api/setup/cli-auth/start")
async def cli_auth_start(body: dict, x_setup_token: str | None = Header(default=None)):
    if not state.daemon_ref or state.daemon_ref._mode_mgr.mode != DaemonMode.INITIALIZE:
        raise HTTPException(status_code=409, detail="Setup only available in initialize mode")
    if not state.setup_token or x_setup_token != state.setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")

    binary = body.get("cli_binary", "claude").strip()
    if not binary or not re.match(r'^[a-zA-Z0-9_./-]+$', binary):
        raise HTTPException(status_code=400, detail="Invalid cli_binary path")

    if state.cli_auth_proc and state.cli_auth_proc.returncode is None:
        try:
            state.cli_auth_proc.kill()
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

    state.cli_auth_proc = proc

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


@router.post("/api/setup/cli-auth/confirm")
async def cli_auth_confirm(body: dict, x_setup_token: str | None = Header(default=None)):
    if not state.daemon_ref or state.daemon_ref._mode_mgr.mode != DaemonMode.INITIALIZE:
        raise HTTPException(status_code=409, detail="Setup only available in initialize mode")
    if not state.setup_token or x_setup_token != state.setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")
    if not state.cli_auth_proc or state.cli_auth_proc.returncode is not None:
        raise HTTPException(status_code=409, detail="No active auth session — call /start first")

    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code required")

    proc = state.cli_auth_proc
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
        state.cli_auth_proc = None

    if returncode != 0:
        out = stdout.decode(errors="replace") if stdout else ""
        raise HTTPException(status_code=500, detail=f"Auth failed (exit {returncode}): {out[:300]}")

    return {"ok": True}


@router.post("/api/cli-auth/start", dependencies=[Depends(verify_token)])
async def runtime_cli_auth_start():
    """Start claude auth login — available in any mode."""
    cfg = config.section("llm") if config._settings else {}
    binary = cfg.get("cli_binary") or "claude"
    if not re.match(r'^[a-zA-Z0-9_./-]+$', binary):
        raise HTTPException(status_code=400, detail="Invalid cli_binary")
    if state.runtime_cli_auth_proc and state.runtime_cli_auth_proc.returncode is None:
        try:
            state.runtime_cli_auth_proc.kill()
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
    state.runtime_cli_auth_proc = proc
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


@router.post("/api/cli-auth/confirm", dependencies=[Depends(verify_token)])
async def runtime_cli_auth_confirm(body: dict):
    """Submit auth code — available in any mode."""
    if not state.runtime_cli_auth_proc or state.runtime_cli_auth_proc.returncode is not None:
        raise HTTPException(status_code=409, detail="No active auth session — call /start first")
    code = body.get("code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="code required")
    proc = state.runtime_cli_auth_proc
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
        state.runtime_cli_auth_proc = None
    if returncode != 0:
        out = stdout.decode(errors="replace") if stdout else ""
        raise HTTPException(status_code=500, detail=f"Auth failed (exit {returncode}): {out[:300]}")
    return {"ok": True}
