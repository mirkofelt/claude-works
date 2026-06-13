import asyncio
import hashlib
import json
import logging
import re
import time
import urllib.parse
from typing import Any

from .. import config
from ..fetcher import fetch_url_content as _fetch_url_content
from ..auth.users import is_admin, is_allowed, upsert_user
from ..kanban.models import KanbanTask
from ..mode import DaemonMode
from ..tasks import tags as _tags
from ..tasks.bundler import merge_content, should_bundle
from ..tasks.models import IncomingMessage
from .renderer import md_to_html as _md_to_telegram_html

logger = logging.getLogger(__name__)

_parse_buttons = _tags.parse_buttons
_URL_RE = re.compile(r'https?://[^\s<>"\']+')
_MAX_FETCH_URLS = 3
_TASK_MAX_CHAT_LEN = 400
_TASK_VERB_RE = re.compile(
    r'\b(schreib|erstell|generier|entwickel|implementier|analysier|recherchier|'
    r'migrier|repari|konvertier|deploy|extrahier|zusammenfass|berechne?|kalkulier|'
    r'write|create|build|develop|implement|generate|analyse|analyze|research|'
    r'migrate|repair|convert|extract|summarize|calculate|deploy)\b',
    re.IGNORECASE,
)


def _user_error(context: str, exc: Exception | None = None) -> str:
    if exc is not None:
        logger.warning("%s: %s", context, exc)
    _FRIENDLY: dict[type, str] = {
        asyncio.TimeoutError: "Zeitüberschreitung.",
    }
    if exc is not None:
        for exc_type, msg in _FRIENDLY.items():
            if isinstance(exc, exc_type):
                return f"⚠️ {context} — {msg}"
    return f"⚠️ {context}."


def _is_task(content: str) -> bool:
    """Returns True if content looks like work to track in kanban, not plain conversation."""
    stripped = content.strip()
    if len(stripped) > _TASK_MAX_CHAT_LEN:
        return True
    if _TASK_VERB_RE.search(stripped):
        return True
    return False



async def handle_message(daemon: Any, msg: dict, is_edited: bool = False) -> None:
    from_user = msg.get("from", {})
    telegram_id = from_user.get("id")
    chat_id = msg["chat"]["id"]
    text = msg.get("text") or msg.get("caption")
    voice = msg.get("voice")

    if not telegram_id:
        return

    msg_type = "voice" if voice else "text"
    logger.info(
        "Message chat=%d user=%d type=%s len=%d edited=%s",
        chat_id, telegram_id, msg_type, len(text or ""), is_edited,
    )

    name = from_user.get("first_name") or from_user.get("username")
    user = await upsert_user(daemon._conn, telegram_id, name)

    if user.get("metadata"):
        try:
            meta = json.loads(user["metadata"]) if isinstance(user["metadata"], str) else user["metadata"]
            background = meta.get("background", "") if isinstance(meta, dict) else ""
        except Exception:
            background = ""
        if background:
            daemon._user_backgrounds[telegram_id] = background

    if user.get("persona"):
        daemon._user_personas[telegram_id] = user["persona"]

    if not await is_allowed(daemon._conn, telegram_id):
        if user["role"] == "blocked":
            await daemon._notify_admin_new_user(telegram_id, name)
        return

    muted = daemon._is_muted(telegram_id)

    if text and text.startswith("/"):
        if muted:
            logger.info("Muted user=%d — command ignored chat=%d", telegram_id, chat_id)
            return
        logger.info("Command %r from user=%d chat=%d", text.split()[0], telegram_id, chat_id)
        await daemon._handle_command(text, telegram_id, chat_id)
        return

    # Check if user is completing CLI re-auth
    if chat_id in daemon._pending_reauth and text and not text.startswith("/"):
        proc = daemon._pending_reauth.pop(chat_id)
        if proc.returncode is not None:
            await daemon._api.send_message(chat_id, "Auth session expired. Run /reauth again.")
            return
        try:
            proc.stdin.write((text.strip() + "\n").encode())
            await proc.stdin.drain()
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode == 0:
                await daemon._api.send_message(chat_id, "✓ Claude CLI authenticated.")
            else:
                await daemon._api.send_message(chat_id, _user_error("Authentifizierung fehlgeschlagen"))
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            await daemon._api.send_message(chat_id, "Auth confirmation timed out. Try /reauth again.")
        return

    # In REPAIR/MIGRATE mode, route messages to Mechanic if admin
    if daemon._mode_mgr.mode in (DaemonMode.REPAIR, DaemonMode.MIGRATE):
        if await is_admin(daemon._conn, telegram_id) and daemon._mechanic and text:
            reply = await daemon._mechanic.followup(text)
            clean_reply, keyboard = _parse_buttons(reply)
            reply_markup = {"inline_keyboard": keyboard} if keyboard else None
            try:
                await daemon._api.send_message(
                    chat_id, _md_to_telegram_html(clean_reply)[:4096],
                    parse_mode="HTML", reply_markup=reply_markup,
                )
            except Exception:
                await daemon._api.send_message(chat_id, clean_reply[:4096], reply_markup=reply_markup)
            return
        await daemon._api.send_message(
            chat_id,
            f"System in {daemon._mode_mgr.mode.value} mode. Please wait.",
        )
        return

    incoming = IncomingMessage(
        telegram_message_id=msg["message_id"],
        chat_id=chat_id,
        from_user_id=telegram_id,
        text=text,
        voice_file_id=voice["file_id"] if voice else None,
        timestamp=msg.get("date", int(time.time())),
        is_edited=is_edited,
    )

    cursor = await daemon._conn.execute(
        """INSERT OR IGNORE INTO messages (telegram_message_id, chat_id, from_user_id, text, voice_file_id, timestamp)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (incoming.telegram_message_id, incoming.chat_id, incoming.from_user_id,
         incoming.text, incoming.voice_file_id, incoming.timestamp),
    )
    await daemon._conn.commit()
    if cursor.rowcount == 0:
        logger.debug("Duplicate message_id=%d — skipping", incoming.telegram_message_id)
        return

    # HARD MUTE gate #2: message is logged to DB but nothing dispatched
    if muted:
        logger.info("Muted user=%d — message logged silently chat=%d", telegram_id, chat_id)
        return

    # Mention-only mode: skip response unless @mentioned or reply-to-bot
    addressed_bot = False
    if chat_id in daemon._mention_only_chats:
        reply_from_id = msg.get("reply_to_message", {}).get("from", {}).get("id", 0)
        is_reply_to_bot = bool(daemon._bot_id and reply_from_id == daemon._bot_id)

        is_mentioned = False
        bot_lower = daemon._bot_username.lower() if daemon._bot_username else ""
        if bot_lower and text:
            entities = msg.get("entities", [])
            for ent in entities:
                if ent.get("type") == "mention":
                    offset, length = ent.get("offset", 0), ent.get("length", 0)
                    mention_text = text[offset:offset + length].lstrip("@").lower()
                    if mention_text == bot_lower:
                        is_mentioned = True
                        break
            if not is_mentioned:
                is_mentioned = f"@{bot_lower}" in text.lower()

        if not is_mentioned and not is_reply_to_bot:
            logger.debug("Mention-only: silently logged msg in chat=%d", chat_id)
            return
        addressed_bot = True
        if bot_lower and text:
            text = re.sub(re.escape(f"@{daemon._bot_username}"), "", text, flags=re.IGNORECASE).strip()

    # GROUP GUARD: loop brake in group chats
    if chat_id < 0 and not await is_admin(daemon._conn, telegram_id):
        if not daemon._group_guard_allows(chat_id, telegram_id):
            return

    pending = daemon._pending_messages.get(chat_id)
    if pending and should_bundle(pending, incoming):
        logger.debug("Bundling message with pending (chat=%d)", chat_id)
        bundled_content = merge_content(pending.text, incoming.text)
        incoming = IncomingMessage(
            telegram_message_id=incoming.telegram_message_id,
            chat_id=chat_id,
            from_user_id=telegram_id,
            text=bundled_content,
            voice_file_id=incoming.voice_file_id or pending.voice_file_id,
            timestamp=incoming.timestamp,
        )

    daemon._pending_messages[chat_id] = incoming

    await asyncio.sleep(2.0)
    if daemon._pending_messages.get(chat_id) is not incoming:
        return

    del daemon._pending_messages[chat_id]
    content = incoming.text or ""
    if incoming.voice_file_id:
        content = await daemon._enrich_voice(incoming.voice_file_id, content)

    if not content.strip():
        logger.info("Empty content chat=%d user=%d — skipping LLM call", chat_id, telegram_id)
        if addressed_bot:
            try:
                await daemon._api.send_message(
                    chat_id,
                    "Leere Nachricht — schreib dazu, was du brauchst.",
                    reply_to_message_id=incoming.telegram_message_id,
                )
            except Exception:
                pass
        return

    urls = _URL_RE.findall(content)
    if urls:
        tor_proxy = config.section("security").get("tor_socks_proxy", "") or None
        fetched_sections: list[str] = []
        urls_blocked: list[str] = []
        for url in urls[:_MAX_FETCH_URLS]:
            page_text = await _fetch_url_content(url, proxy=tor_proxy)
            if page_text is not None:
                fetched_sections.append(f"[Content of {url}]\n{page_text}")
                daemon._track_payloads(chat_id, [page_text])
                logger.debug("Fetched URL via Tor: %s (%d chars)", url, len(page_text))
            else:
                urls_blocked.append(url)
                logger.info("URL blocked Tor or Tor unavailable: %s", url)
        if fetched_sections:
            content = content + "\n\n## Fetched Web Content\n" + "\n\n---\n\n".join(fetched_sections)
        if urls_blocked:
            fetch_hash = hashlib.sha256(
                f"{chat_id}:{telegram_id}:{','.join(urls_blocked)}:{time.time()}".encode()
            ).hexdigest()[:8]
            daemon._pending_direct_fetches[fetch_hash] = {
                "chat_id": chat_id,
                "user_id": telegram_id,
                "content": content,
                "urls": urls_blocked,
                "expires_at": time.time() + 300,
            }
            domains = ", ".join(urllib.parse.urlparse(u).netloc for u in urls_blocked)
            await daemon._api.send_message(
                chat_id,
                f"🔒 Tor access failed: <code>{domains}</code>\nAllow direct access?",
                parse_mode="HTML",
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Yes", "callback_data": f"direct:{fetch_hash}"},
                    {"text": "❌ Skip", "callback_data": f"deny:{fetch_hash}"},
                ]]}
            )
            return

    if chat_id < 0:
        daemon._record_group_reply(chat_id, telegram_id)

    if _is_task(content):
        task_content = content
        recent = await daemon._load_chat_history(chat_id, limit=10)
        if recent:
            ctx_lines = []
            for m in recent:
                prefix = "User" if m["role"] == "user" else "Bot"
                ctx_lines.append(f"{prefix}: {m['content'][:300]}")
            task_content = "## Recent conversation\n" + "\n".join(ctx_lines) + "\n\n---\n\n" + content
        task = KanbanTask(
            id=None,
            chat_id=chat_id,
            user_id=telegram_id,
            content=task_content,
            priority=1 if content.startswith("!") else 0,
        )
        task_id = await daemon._board.push(task)
        try:
            await daemon._api.set_message_reaction(chat_id, incoming.telegram_message_id, "⏳")
            daemon._pending_reactions[task_id] = (chat_id, incoming.telegram_message_id)
            await daemon._conn.execute(
                "INSERT OR REPLACE INTO pending_reactions (task_id, chat_id, tg_msg_id) VALUES (?, ?, ?)",
                (task_id, chat_id, incoming.telegram_message_id),
            )
            await daemon._conn.commit()
        except Exception:
            pass
        try:
            preview = content[:120] + ("…" if len(content) > 120 else "")
            init_sent = await daemon._api.send_message(
                chat_id, f"✎ Working on: {preview}",
                reply_to_message_id=incoming.telegram_message_id,
            )
            init_msg_id = init_sent["message_id"]
            daemon._pending_initial_msgs[task_id] = init_msg_id
            await daemon._conn.execute(
                "INSERT OR REPLACE INTO pending_initial_msgs (task_id, chat_id, tg_msg_id) VALUES (?, ?, ?)",
                (task_id, chat_id, init_msg_id),
            )
            await daemon._conn.commit()
        except Exception:
            pass
    else:
        if chat_id in daemon._typing_tasks:
            daemon._pending_chat_queue.setdefault(chat_id, []).append(
                (telegram_id, content, incoming.telegram_message_id)
            )
            if incoming.telegram_message_id:
                try:
                    await daemon._api.set_message_reaction(chat_id, incoming.telegram_message_id, "⏳")
                except Exception:
                    pass
        else:
            daemon._active_chat_content[chat_id] = content
            daemon._chat_task_start_times[chat_id] = time.time()
            asyncio.create_task(
                daemon._handle_chat(chat_id, telegram_id, content, incoming.telegram_message_id),
                name=f"chat-{chat_id}",
            )
