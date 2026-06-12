"""KB-Watch cron job.

Periodically scans the knowledge base for entries that need attention:
- Entries flagged as stale (not updated for N days)
- Entries in quarantine (pending trust review)
- Empty or malformed entries

On each tick a cheap LLM pass reviews flagged entries and either:
- Marks them as reviewed (no action needed)
- Generates an update suggestion sent to admins via notify()

Config (daemon_config)::

    "cron": {
      "kb_watch": {
        "enabled": true,
        "interval_minutes": 360,
        "stale_days": 90,
        "max_per_tick": 10
      }
    }
"""
import logging
import time

from .. import config
from ..config import get_agent_model
from ..cron import CronContext
from ..llm.provider import get_provider

logger = logging.getLogger(__name__)

JOB_NAME = "kb_watch"


async def kb_watch(ctx: CronContext, state: dict) -> dict | None:
    job_cfg = ctx.job_cfg
    stale_days = job_cfg.get("stale_days", 90)
    max_per_tick = job_cfg.get("max_per_tick", 10)
    stale_cutoff = int(time.time()) - stale_days * 86400

    # Find stale entries (not updated in stale_days) and quarantined entries
    async with ctx.conn.execute(
        """SELECT id, title, type, tags, content, updated_at
           FROM knowledge
           WHERE (updated_at IS NULL OR updated_at < ?)
              OR quarantined = 1
           ORDER BY updated_at ASC NULLS FIRST
           LIMIT ?""",
        (stale_cutoff, max_per_tick),
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        logger.info("KB watch: nothing stale or quarantined")
        return state

    quarantined = [r for r in rows if r["quarantined"] if "quarantined" in r.keys()]
    stale = [r for r in rows if r not in quarantined]

    report_parts = []

    if quarantined:
        report_parts.append(f"**{len(quarantined)} KB-Einträge in Quarantäne** (von unvertrauten Chats):")
        for r in quarantined:
            report_parts.append(f"  • ID:{r['id']} [{r['type']}] **{r['title']}**")

    if stale:
        report_parts.append(f"**{len(stale)} KB-Einträge möglicherweise veraltet** (>{stale_days} Tage nicht aktualisiert):")
        for r in stale:
            age = (int(time.time()) - (r["updated_at"] or 0)) // 86400
            report_parts.append(f"  • ID:{r['id']} [{r['type']}] **{r['title']}** ({age}d alt)")

    if report_parts:
        msg = "🗂️ KB-Watch:\n" + "\n".join(report_parts)
        await ctx.notify(msg)
        logger.info("KB watch: notified admins about %d entries", len(rows))

    return {**state, "last_run": int(time.time()), "flagged": len(rows)}
