import hashlib
import hmac

from fastapi import HTTPException, Request

from .. import config
from . import state


def client_ip(request: Request) -> str:
    cfg = config.section("web") if config._settings else {}
    if cfg.get("trusted_proxy"):
        cf = request.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip()
        fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if fwd:
            return fwd
    return request.client.host if request.client else "unknown"


def is_https(request: Request) -> bool:
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    return proto == "https"


def verify_token(request: Request) -> None:
    ip = client_ip(request)
    if not state.api_limiter.hit(ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded — slow down")
    cfg = config.section("web")
    raw_token = cfg.get("auth_token", "")
    if not raw_token:
        raise HTTPException(status_code=503, detail="Auth not configured")
    expected = hashlib.sha256(raw_token.encode()).hexdigest()
    token = request.headers.get("X-Auth-Token") or request.cookies.get("auth") or ""
    if not hmac.compare_digest(token, expected):
        if not state.auth_fail_limiter.hit(ip):
            raise HTTPException(status_code=429, detail="Too many failed auth attempts — locked out for 5 minutes")
        raise HTTPException(status_code=401, detail="Unauthorized")


async def get_conn():
    from .. import db
    return await db.get_conn()
