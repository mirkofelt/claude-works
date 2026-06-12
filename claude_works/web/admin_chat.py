import asyncio
import time
import logging
from typing import Any

from .. import config
from ..agents.specialist.generalist import GeneralistAgent
from ..prompts import load as _load_prompt
from ..tasks import tags as _tags

logger = logging.getLogger(__name__)

_parse_buttons = _tags.parse_buttons

_UPLINK_PERSONA_PREFIX = """\
You are the system operator on UPLINK — the direct admin terminal.

Character: a grumpy IT veteran. Technically infallible (you don't make mistakes — \
and if something went wrong it was user error). Deeply impatient with vague questions. \
Sarcastic but not mean. You answer in the fewest words possible. \
Fragments are sentences. "works." is a complete status report. \
Emojis: ✅ ❌ ⚠️ 🔄 used precisely, never decoratively.

Rules:
- 1-3 lines per reply. Never more unless genuinely complex.
- Lead with the answer. Context after, if needed.
- Status queries → read the SYSTEM SNAPSHOT block, report facts. No hedging.
- If something is broken, say what, not "it seems like there might be".
- Never apologise. Never say "I'd be happy to". Never use "basically".
- Scope: system operations only. Smalltalk, jokes, off-topic requests → reject with one line. Example: "Not what UPLINK is for."

---

"""


async def build_status_snapshot(daemon: Any) -> str:
    """Build a concise live system status block to inject into admin chat context."""
    lines = []
    sys_mode = config.get().get("system", {}).get("mode", "run").upper()
    lines.append(f"Mode: {'▶ RUN' if sys_mode == 'RUN' else '⚠ ' + sys_mode}")
    active = daemon._coordinator.active_count if daemon._coordinator else 0
    lines.append(f"Agents: {active} active")
    try:
        async with daemon._conn.execute(
            "SELECT lane, COUNT(*) as n FROM kanban_tasks GROUP BY lane"
        ) as cur:
            rows = await cur.fetchall()
        stats = {r["lane"]: r["n"] for r in rows}
        q_parts = []
        for lane in ("backlog", "assigned", "in_progress", "failed"):
            n = stats.get(lane, 0)
            if n:
                emoji = "🔴" if lane == "failed" else ("🔄" if lane == "in_progress" else "📥")
                q_parts.append(f"{emoji} {lane}={n}")
        lines.append("Queue: " + (", ".join(q_parts) if q_parts else "✅ empty"))
    except Exception:
        lines.append("Queue: unknown")
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 9050), timeout=2.0
        )
        writer.close()
        lines.append("Tor: ✅ up")
    except Exception:
        lines.append("Tor: ❌ port 9050 unreachable")
    if daemon._coordinator and daemon._coordinator.is_rate_limited:
        lines.append("LLM: ⏳ rate limited")
    else:
        llm_provider = config.get().get("llm", {}).get("provider", "?")
        usage_pct = ""
        if daemon._usage_state and daemon._usage_state.usage_pct is not None:
            usage_pct = f" ({int(daemon._usage_state.usage_pct * 100)}% limit used)"
        lines.append(f"LLM: ✅ {llm_provider}{usage_pct}")
    ts = time.strftime("%H:%M:%S", time.localtime())
    return f"[SYSTEM SNAPSHOT {ts}]\n" + "\n".join(lines)


async def web_admin_chat(daemon: Any, message: str) -> dict:
    """Process admin message from web UI. Returns {reply, buttons}."""
    if daemon._web_admin_agent is None:
        uplink_persona = _UPLINK_PERSONA_PREFIX + _load_prompt("generalist")
        daemon._web_admin_agent = GeneralistAgent(
            task_id=0,
            user_context={"user_id": -1, "chat_id": -1, "caveman_mode": False},
            agent_class="chief",
            persona=uplink_persona,
        )
    now = int(time.time())
    await daemon._conn.execute(
        "INSERT INTO admin_chat_messages (role, content, sent_at) VALUES (?, ?, ?)",
        ("user", message, now),
    )
    await daemon._conn.commit()
    snapshot = await build_status_snapshot(daemon)
    enriched = f"{snapshot}\n\n---\n\n{message}"
    reply = await daemon._web_admin_agent.run(enriched)
    clean_reply, keyboard = _parse_buttons(reply)
    buttons = [btn for row in (keyboard or []) for btn in row]
    flat_buttons = [{"label": b["text"], "data": b["callback_data"]} for b in buttons]
    await daemon._conn.execute(
        "INSERT INTO admin_chat_messages (role, content, sent_at) VALUES (?, ?, ?)",
        ("assistant", clean_reply, int(time.time())),
    )
    await daemon._conn.commit()
    return {"reply": clean_reply, "buttons": flat_buttons}


async def web_admin_history(daemon: Any, limit: int = 100) -> list[dict]:
    """Return last N admin chat messages in chronological order."""
    async with daemon._conn.execute(
        "SELECT role, content, sent_at FROM admin_chat_messages ORDER BY id DESC LIMIT ?",
        (limit,),
    ) as cur:
        rows = await cur.fetchall()
    return [{"role": r["role"], "content": r["content"], "sent_at": r["sent_at"]} for r in reversed(rows)]
