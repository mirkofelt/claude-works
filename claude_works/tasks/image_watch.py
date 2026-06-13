"""Image-watch cron job.

Polls ghcr.io for the latest image digest. When a new digest is detected
and notifications are not muted, sends Telegram buttons to admin(s):
  ✅ Ja            — trigger redeploy via claude-guard
  🔕 1h stumm     — suppress for 60 minutes
  🔇 Heute stumm  — suppress until midnight Europe/Berlin

State keys (persisted in cron_jobs.state_json):
  last_digest   str  — last seen digest (sha256:...)
  mute_until    int  — Unix epoch; skip notifications while now < mute_until
"""

import base64
import logging
import time

import httpx

from .. import config
from ..cron import CronContext

logger = logging.getLogger(__name__)

JOB_NAME = "image_watch"
CALLBACK_PREFIX = "imgwatch"

_DEFAULT_IMAGE = "ghcr.io/mirkofelt/claude-works"
_DEFAULT_TAG = "latest"


async def image_watch(ctx: CronContext, state: dict) -> dict:
    mute_until = int(state.get("mute_until", 0))
    if mute_until and time.time() < mute_until:
        return state

    cfg = ctx.job_cfg
    image_ref = cfg.get("image", _DEFAULT_IMAGE)
    tag = cfg.get("tag", _DEFAULT_TAG)

    digest = await _fetch_digest(image_ref, tag)
    if not digest:
        raise RuntimeError(f"Could not fetch digest for {image_ref}:{tag}")

    last = state.get("last_digest", "")
    if not last:
        state["last_digest"] = digest
        await ctx.save_state(state)
        logger.info("image_watch: baseline initialized %s", digest[:16])
        return state

    if digest == last:
        return state

    short = digest.split(":")[-1][:12]
    state["last_digest"] = digest
    await ctx.save_state(state)

    msg = f"🐳 Neues Image: <code>{short}</code> — Redeploy?"
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Ja", "callback_data": f"{CALLBACK_PREFIX}_deploy:{short}"},
            {"text": "🔕 1h stumm", "callback_data": f"{CALLBACK_PREFIX}_mute_1h:{short}"},
            {"text": "🔇 Heute stumm", "callback_data": f"{CALLBACK_PREFIX}_mute_today:{short}"},
        ]]
    }
    await ctx.notify_rich(msg, parse_mode="HTML", reply_markup=markup)
    return state


async def _fetch_digest(image_ref: str, tag: str) -> str | None:
    parts = image_ref.split("/", 1)
    registry = parts[0]
    repo_path = parts[1] if len(parts) > 1 else image_ref

    gh_cfg = config.section("github")
    pat = gh_cfg.get("token", "")

    token = await _get_registry_token(registry, repo_path, pat)
    if not token:
        logger.warning("image_watch: could not get registry token")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": (
            "application/vnd.oci.image.index.v1+json,"
            "application/vnd.docker.distribution.manifest.list.v2+json,"
            "application/vnd.docker.distribution.manifest.v2+json"
        ),
    }
    url = f"https://{registry}/v2/{repo_path}/manifests/{tag}"
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
        if r.status_code == 200:
            return r.headers.get("Docker-Content-Digest", "")
        logger.warning("image_watch: manifest HTTP %d for %s", r.status_code, url)
        return None
    except Exception as e:
        logger.warning("image_watch: manifest request failed: %s", e)
        return None


async def _get_registry_token(registry: str, repo: str, pat: str) -> str | None:
    if not pat:
        return None
    cred = base64.b64encode(f"token:{pat}".encode()).decode()
    url = f"https://{registry}/token"
    params = {"service": registry, "scope": f"repository:{repo}:pull"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, params=params, headers={"Authorization": f"Basic {cred}"})
        if r.status_code == 200:
            return r.json().get("token", "")
        logger.warning("image_watch: token HTTP %d", r.status_code)
        return None
    except Exception as e:
        logger.warning("image_watch: token fetch failed: %s", e)
        return None
