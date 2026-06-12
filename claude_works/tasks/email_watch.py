"""Email-watch cron job.

Polls an IMAP folder for new mail (UID-based, stable across restarts) and
notifies the admins about messages worth their attention. State lives in
``cron_jobs.state_json`` ({"last_uid": int}) — durable, survives restarts,
no expiry, unlike a session-scoped Claude cron.

Relevance: by default each new mail is judged by a cheap LLM pass — only
mail that warrants attention is forwarded, the rest stays silent. The filter
is fail-open: if the LLM errors, the mail is forwarded rather than dropped
(missing a real mail is worse than an occasional false positive). Set
``cron.email_watch.relevance_filter = false`` to forward every new mail.

Config (daemon_config)::

    "cron": {
      "email_watch": {
        "enabled": true,
        "interval_minutes": 60,
        "folder": "INBOX",
        "relevance_filter": true,
        "max_per_tick": 15,
        "snippet_chars": 1200
      }
    }

IMAP credentials are read from the existing ``email`` config section
(imap_host / imap_port / imap_user / imap_password) — same as READ_EMAIL.
"""

import json
import logging

from .. import config
from ..config import get_agent_model
from ..cron import CronContext
from ..llm.provider import get_provider
from .email import read_new_emails

logger = logging.getLogger(__name__)

JOB_NAME = "email_watch"

DEFAULT_FOLDER = "INBOX"
DEFAULT_MAX_PER_TICK = 15
DEFAULT_SNIPPET_CHARS = 1200

_RELEVANCE_SYSTEM = (
    "Du bist ein Mail-Filter für einen vielbeschäftigten Nutzer. "
    "Beurteile, ob diese E-Mail eine Benachrichtigung rechtfertigt. "
    "Relevant: persönliche Nachrichten, Rechnungen, Fristen, Sicherheitswarnungen, "
    "Termine, alles was eine Handlung oder Aufmerksamkeit erfordert. "
    "Nicht relevant: Newsletter, Werbung, automatische Benachrichtigungen, Spam, "
    "Social-Media-Updates. "
    'Antworte AUSSCHLIESSLICH mit JSON: {"relevant": true|false, "reason": "<max 8 Wörter>"}'
)


async def email_watch(ctx: CronContext, state: dict) -> dict:
    folder = ctx.job_cfg.get("folder", DEFAULT_FOLDER)
    max_per_tick = int(ctx.job_cfg.get("max_per_tick", DEFAULT_MAX_PER_TICK))
    snippet_chars = int(ctx.job_cfg.get("snippet_chars", DEFAULT_SNIPPET_CHARS))
    relevance_filter = bool(ctx.job_cfg.get("relevance_filter", True))

    email_cfg = config.section("email")
    if not email_cfg.get("imap_host"):
        raise RuntimeError("email.imap_host nicht konfiguriert — Email-Watch nicht möglich")

    last_uid = int(state.get("last_uid", 0))
    result = await read_new_emails(folder, last_uid, max_per_tick, snippet_chars, email_cfg)
    max_uid = int(result["max_uid"])
    messages = result["messages"]

    if last_uid <= 0:
        # First run: arm the watcher at the current high-water mark, no backlog flood.
        state["last_uid"] = max_uid
        await ctx.notify(
            f"📬 Email-Watch aktiv ({folder}). Baseline bei UID {max_uid}. "
            "Ab jetzt melde ich nur neue, relevante Mail."
        )
        return state

    if not messages:
        state["last_uid"] = max_uid
        return state  # nothing new — silent

    if relevance_filter:
        relevant = await _filter_relevant(messages)
    else:
        relevant = [(m, "") for m in messages]

    if relevant:
        await ctx.notify(_format_digest(relevant, folder, relevance_filter))

    # Advance the high-water mark only after a successful notify. If notify
    # raises, the handler errors, state is NOT saved, and the same mails are
    # re-evaluated next tick — no silent loss.
    state["last_uid"] = max_uid
    return state


async def _filter_relevant(messages: list[dict]) -> list[tuple[dict, str]]:
    """Judge each mail; return [(mail, reason)] for the relevant ones. Fail-open."""
    provider = get_provider(config.section("llm"))
    model = get_agent_model("default")
    out: list[tuple[dict, str]] = []
    try:
        for m in messages:
            reason = await _judge_one(provider, model, m)
            if reason is not None:
                out.append((m, reason))
    finally:
        try:
            await provider.close()
        except Exception:
            logger.exception("email_watch: provider close failed")
    return out


async def _judge_one(provider, model: str, m: dict) -> str | None:
    """Return a short reason string if relevant, else None. Fail-open on error."""
    prompt = (
        f"Von: {m['from']}\n"
        f"Betreff: {m['subject']}\n"
        f"Auszug: {m['snippet']}"
    )
    try:
        response = await provider.complete(
            [{"role": "user", "content": prompt}],
            system=_RELEVANCE_SYSTEM,
            model=model,
            max_tokens=128,
        )
        data = json.loads(response.text.strip())
        if bool(data.get("relevant", False)):
            return str(data.get("reason", ""))[:80]
        return None
    except Exception as e:
        logger.warning("email_watch: relevance judge failed for uid=%s: %s — fail-open", m.get("uid"), e)
        return "(Filter-Fehler — vorsichtshalber gemeldet)"


def _format_digest(relevant: list[tuple[dict, str]], folder: str, filtered: bool) -> str:
    n = len(relevant)
    label = "relevante" if filtered else "neue"
    lines = [f"📬 {n} {label} Mail{'s' if n != 1 else ''} in {folder}:"]
    for m, reason in relevant:
        frm = m["from"][:60]
        subj = m["subject"][:80]
        lines.append(f"• {frm}\n  {subj}")
        if reason:
            lines.append(f"  → {reason}")
    return "\n".join(lines)
