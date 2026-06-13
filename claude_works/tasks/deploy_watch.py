"""Deploy-watch cron job.

Polls the GitHub repo's main branch for a new commit SHA. On change:
persist new baseline (DB + KB reference entry), notify admins, trigger
a redeploy via claude-guard — same path as the manual deploy trigger.

Baseline lives in cron_jobs.state_json ({"baseline_sha": ...}), mirrored
into the knowledge base entry 'deploy-watch-baseline' for human reference.
"""

import logging
import time

from .. import config
from ..cron import CronContext
from .github import github_api

logger = logging.getLogger(__name__)

JOB_NAME = "deploy_watch"
KB_TITLE = "deploy-watch-baseline"

DEFAULT_REPO = "mirkofelt/claude-works"
DEFAULT_BRANCH = "main"


async def deploy_watch(ctx: CronContext, state: dict) -> dict:
    repo = ctx.job_cfg.get("repo", DEFAULT_REPO)
    branch = ctx.job_cfg.get("branch", DEFAULT_BRANCH)

    gh_cfg = config.section("github")
    data = await github_api("GET", f"/repos/{repo}/commits/{branch}", None, gh_cfg)
    sha = data.get("sha", "")
    if not sha:
        raise RuntimeError(f"GitHub API lieferte keinen SHA für {repo}@{branch}")

    baseline = state.get("baseline_sha", "")

    if not baseline:
        # First run ever: initialize baseline, no deploy.
        state["baseline_sha"] = sha
        await ctx.save_state(state)
        await _update_kb_baseline(ctx.conn, sha, repo, branch)
        await ctx.notify(f"Deploy-Watch: Baseline initialisiert auf {sha[:7]} ({repo}@{branch}).")
        return state

    if sha == baseline:
        return state  # no new commit — silent

    commit_msg = (data.get("commit", {}).get("message") or "").splitlines()[0][:120]

    # Persist baseline BEFORE triggering the deploy: the deploy restarts this
    # container — persisting afterwards would race the restart and re-trigger
    # the same deploy forever.
    state["baseline_sha"] = sha
    await ctx.save_state(state)
    await _update_kb_baseline(ctx.conn, sha, repo, branch)
    await ctx.notify(
        f"🚀 Deploy-Watch: neuer Commit auf {branch}\n"
        f"{baseline[:7]} → {sha[:7]}: {commit_msg}\n"
        f"Redeploy wird ausgelöst."
    )
    await _trigger_deploy()
    return state


async def sync_baseline(conn, repo: str = DEFAULT_REPO, branch: str = DEFAULT_BRANCH) -> None:
    """Update deploy_watch baseline to current GitHub HEAD.

    Call before any external deploy trigger (e.g. image_watch) so that
    deploy_watch doesn't re-trigger on the same commit after restart.
    """
    import json
    gh_cfg = config.section("github")
    try:
        data = await github_api("GET", f"/repos/{repo}/commits/{branch}", None, gh_cfg)
        sha = data.get("sha", "")
        if not sha:
            return
        async with conn.execute(
            "SELECT state_json FROM cron_jobs WHERE name = ?", (JOB_NAME,)
        ) as cur:
            row = await cur.fetchone()
        state = json.loads((row["state_json"] if row else None) or "{}")
        if state.get("baseline_sha") == sha:
            return  # already up to date
        state["baseline_sha"] = sha
        await conn.execute(
            "UPDATE cron_jobs SET state_json = ?, updated_at = ? WHERE name = ?",
            (json.dumps(state), int(time.time()), JOB_NAME),
        )
        await conn.commit()
        logger.info("deploy_watch baseline synced to %s", sha[:7])
    except Exception as e:
        logger.warning("deploy_watch baseline sync failed: %s", e)


async def _trigger_deploy() -> None:
    """Trigger redeploy via claude-guard — same path as the manual deploy trigger."""
    import httpx

    dg = config.section("system").get("claude_guard", {})
    guard_url = dg.get("url", "").rstrip("/")
    token = dg.get("token", "")
    if not guard_url or not token:
        raise RuntimeError("claude_guard.url/.token nicht konfiguriert — Redeploy nicht möglich")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(f"{guard_url}/deploy?token={token}")
    except (httpx.TransportError, httpx.TimeoutException) as e:
        # Expected: the deploy restarts this container, the connection may
        # drop before claude-guard answers. Startup notification confirms.
        logger.info("claude-guard connection dropped during deploy (likely restart): %s", e)
        return

    if r.status_code != 200:
        raise RuntimeError(f"claude-guard /deploy HTTP {r.status_code}: {r.text[:300]}")


async def _update_kb_baseline(conn, sha: str, repo: str, branch: str) -> None:
    """Mirror baseline into KB entry 'deploy-watch-baseline' (reference for humans/agents)."""
    from ..knowledge import store as kb

    content = (
        f"Letzter deployter Stand: {sha} ({repo}@{branch}).\n"
        f"Aktualisiert: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        f"Wird vom Deploy-Watch-Cron-Job bei jedem Redeploy automatisch aktualisiert."
    )
    try:
        async with conn.execute(
            "SELECT id FROM knowledge WHERE title = ? LIMIT 1", (KB_TITLE,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            await kb.update(conn, row["id"], content=content)
        else:
            await kb.add(
                conn, title=KB_TITLE, content=content,
                type="fact", tags=["deploy", "cron"], source="cron::deploy_watch",
            )
    except Exception:
        # KB mirror is a reference, not the source of truth — never fail the job over it.
        logger.exception("KB baseline mirror update failed")
