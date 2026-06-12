import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ... import config, db
from ...config_store import save_config as _store_save_config
from .. import state
from ..deps import verify_token

router = APIRouter()

_WHITELIST_TYPES = ("github_merge", "github_api_write", "send_email", "config_put")
_WHITELIST_MATCHER_FIELDS = {
    "github_merge": ("repo", "branch"),
    "github_api_write": ("method", "endpoint"),
    "send_email": ("recipient", "domain"),
    "config_put": ("key", "key_prefix"),
}

_GROUP_FIELDS = ("persona", "focus", "communication_style", "echo_filter", "truncation_limit", "model_override")
_GROUP_TEXT_FIELDS = ("persona", "focus", "communication_style", "model_override")
_GROUP_NUMERIC_FIELDS = ("truncation_limit",)
_GROUP_BOOL_FIELDS = ("echo_filter",)


def _whitelist_rules() -> list[dict]:
    rules = config.section("whitelist").get("rules")
    return [dict(r) for r in rules] if isinstance(rules, list) else []


async def _save_whitelist_rules(rules: list[dict]) -> None:
    cfg = dict(config.get())
    wl = dict(cfg.get("whitelist") or {})
    wl["rules"] = rules
    cfg["whitelist"] = wl
    conn = await db.init_config()
    await _store_save_config(conn, cfg)
    await conn.close()
    config.set(cfg)


def _validate_whitelist_rule(body: dict) -> dict:
    rtype = body.get("type")
    if rtype not in _WHITELIST_TYPES:
        raise HTTPException(status_code=400, detail=f"type must be one of {_WHITELIST_TYPES}")
    matcher_in = body.get("matcher")
    if not isinstance(matcher_in, dict):
        raise HTTPException(status_code=400, detail="matcher must be an object")
    allowed = _WHITELIST_MATCHER_FIELDS[rtype]
    matcher: dict = {}
    for k, v in matcher_in.items():
        if k not in allowed:
            raise HTTPException(status_code=400, detail=f"unknown matcher field '{k}' for type '{rtype}'")
        if v is None:
            continue
        s = str(v).strip()
        if s:
            matcher[k] = s
    if not matcher:
        raise HTTPException(status_code=400, detail=f"matcher must constrain at least one of {allowed}")
    return {"type": rtype, "matcher": matcher, "enabled": bool(body.get("enabled", True))}


async def _require_meta_approval(summary: str) -> None:
    """Block until a supervisor approves a whitelist change. 403 on deny."""
    if not state.daemon_ref or not getattr(state.daemon_ref, "_security", None):
        raise HTTPException(status_code=503, detail="Security supervisor not available")
    approved = await state.daemon_ref._security.require_approval(
        ["whitelist_change"], summary,
    )
    if not approved:
        raise HTTPException(status_code=403, detail="Whitelist change denied by supervisor")


def _parse_group_id(raw: Any) -> int:
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="chat_id must be an integer")
    if cid >= 0:
        raise HTTPException(status_code=400, detail="chat_id must be negative (a group)")
    return cid


def _validate_group_field(name: str, value: Any) -> Any | None:
    if value is None:
        return None
    if name in _GROUP_TEXT_FIELDS:
        v = str(value).strip()
        return v if v else None
    if name in _GROUP_BOOL_FIELDS:
        result: bool
        if isinstance(value, bool):
            result = value
        elif isinstance(value, str):
            result = value.lower() in ('true', '1', 'yes', 'on')
        else:
            result = bool(value)
        return result if result else None
    if name in _GROUP_NUMERIC_FIELDS:
        try:
            v = int(value)
            if v < 0:
                raise HTTPException(status_code=400, detail=f"{name} must be a non-negative integer")
            return v if v > 0 else None
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{name} must be a non-negative integer")
    return None


async def _save_groups(groups: dict) -> None:
    cfg = dict(config.get())
    cfg["groups"] = groups
    conn = await db.init_config()
    await _store_save_config(conn, cfg)
    await conn.close()
    config.set(cfg)


@router.get("/api/config", dependencies=[Depends(verify_token)])
async def get_config():
    cfg = config.get()
    return {"config": cfg}


@router.get("/api/plugins/config", dependencies=[Depends(verify_token)])
async def get_plugins_config():
    return config.get().get("plugins", {})


@router.get("/api/plugins/config/{plugin_name}", dependencies=[Depends(verify_token)])
async def get_plugin_config(plugin_name: str):
    plugins = config.get().get("plugins", {})
    if plugin_name not in plugins:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' not configured")
    return plugins[plugin_name]


@router.post("/api/config/save", dependencies=[Depends(verify_token)])
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


@router.post("/api/config/reload", dependencies=[Depends(verify_token)])
async def reload_config():
    from ...config_store import load_config as _load_db_cfg
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


@router.get("/api/whitelist", dependencies=[Depends(verify_token)])
async def list_whitelist():
    return {"rules": _whitelist_rules()}


@router.post("/api/whitelist", dependencies=[Depends(verify_token)])
async def add_whitelist_rule(body: dict):
    rule = _validate_whitelist_rule(body)
    await _require_meta_approval(f"Whitelist ADD: {rule['type']} {rule['matcher']}")
    rule["id"] = uuid.uuid4().hex[:8]
    rules = _whitelist_rules()
    rules.append(rule)
    try:
        await _save_whitelist_rules(rules)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "rule": rule}


@router.delete("/api/whitelist/{rule_id}", dependencies=[Depends(verify_token)])
async def delete_whitelist_rule(rule_id: str):
    rules = _whitelist_rules()
    target = next((r for r in rules if r.get("id") == rule_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No whitelist rule '{rule_id}'")
    await _require_meta_approval(f"Whitelist DELETE: {target.get('type')} {target.get('matcher')}")
    remaining = [r for r in rules if r.get("id") != rule_id]
    try:
        await _save_whitelist_rules(remaining)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "deleted": rule_id}


@router.get("/api/groups", dependencies=[Depends(verify_token)])
async def list_groups():
    return {"groups": config.section("groups")}


@router.post("/api/groups", dependencies=[Depends(verify_token)])
async def upsert_group(body: dict):
    cid = _parse_group_id(body.get("chat_id"))
    entry = {}
    for f in _GROUP_FIELDS:
        validated = _validate_group_field(f, body.get(f))
        if validated is not None:
            entry[f] = validated
    groups = dict(config.section("groups"))
    groups[str(cid)] = entry
    groups.pop(cid, None)
    try:
        await _save_groups(groups)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "chat_id": str(cid), "group": entry}


@router.delete("/api/groups/{chat_id}", dependencies=[Depends(verify_token)])
async def delete_group(chat_id: str):
    cid = _parse_group_id(chat_id)
    groups = dict(config.section("groups"))
    existed = groups.pop(str(cid), None)
    if groups.pop(cid, None) is not None:
        existed = True
    if existed is None:
        raise HTTPException(status_code=404, detail=f"Group {cid} not configured")
    try:
        await _save_groups(groups)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "deleted": str(cid)}
