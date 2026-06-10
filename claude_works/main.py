import asyncio
import logging
import re
from .tasks.transcriber import transcribe as _transcribe_audio
import secrets
import time
import os
import signal
from typing import Any

import uvicorn

from . import config, db
from .mode import DaemonMode, ModeManager, detect_startup_mode
from .telegram.api import TelegramAPI
from .telegram.poller import TelegramPoller
from .telegram.reactions import resolve_action, extract_reaction_emoji
from .tasks.models import IncomingMessage
from .tasks.bundler import should_bundle, merge_content
from .kanban.board import KanbanBoard
from .kanban.models import KanbanTask
from .telemetry.tokens import TokenTracker
from .knowledge import store as knowledge_store
from .agents.coordinator import AgentCoordinator
from .agents.mechanic import MechanicAgent, MechanicContext
from .agents.specialist.generalist import GeneralistAgent
from .auth.users import upsert_user, is_allowed, is_admin, set_role
from .memory import store as memory_store
from .security import SecuritySupervisor
from .web.app import app as web_app, set_daemon as _set_web_daemon, set_setup_token as _set_web_setup_token
from .logging_setup import setup as _setup_logging, uvicorn_log_config as _uvicorn_log_config

logger = logging.getLogger(__name__)

TYPING_INTERVAL = 4.0
PID_FILE = "/data/claude-works.pid"


def _md_to_telegram_html(text: str) -> str:
    """Convert Markdown subset to Telegram HTML. Escapes & < > in text nodes."""
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts = re.split(r"```(?:[^\n`]*)\n?([\s\S]*?)```", text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append(f"<pre>{esc(part.strip())}</pre>")
        else:
            segs = re.split(r"`([^`\n]+)`", part)
            for j, seg in enumerate(segs):
                if j % 2 == 1:
                    result.append(f"<code>{esc(seg)}</code>")
                else:
                    s = esc(seg)
                    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=re.DOTALL)
                    result.append(s)
    return "".join(result)


def _extract_voice_tag(text: str) -> "tuple[str, str | None]":
    """Extract [VOICE: text] tag. Returns (clean_text, tts_text or None)."""
    m = re.search(r'\[VOICE:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    return clean, m.group(1).strip()


def _extract_map_tag(text: str) -> "tuple[str, str | None]":
    """Extract [MAP: query] tag. Returns (clean_text, map_query or None)."""
    m = re.search(r'\[MAP:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    return clean, m.group(1).strip()


def _parse_buttons(text: str) -> "tuple[str, list[list[dict]] | None]":
    """Extract [BUTTONS: ...] tag from text. Returns (clean_text, inline_keyboard or None).
    Format: [BUTTONS: label1|data1, label2|data2, ...]
    Buttons are laid out in rows of max 3."""
    m = re.search(r'\[BUTTONS:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    clean = text[:m.start()].rstrip() + text[m.end():]
    specs = [s.strip() for s in m.group(1).split(',')]
    buttons = []
    for spec in specs:
        parts = spec.split('|', 1)
        label = parts[0].strip()
        data = parts[1].strip() if len(parts) > 1 else label
        buttons.append({"text": label, "callback_data": data[:64]})
    rows = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    return clean.strip(), rows


class Daemon:
    def __init__(self) -> None:
        self._conn: Any = None
        self._api: TelegramAPI | None = None
        self._poller: TelegramPoller | None = None
        self._board: KanbanBoard | None = None
        self._token_tracker: TokenTracker | None = None
        
        self._coordinator: AgentCoordinator | None = None
        self._web_server: uvicorn.Server | None = None
        self._security: SecuritySupervisor = SecuritySupervisor()
        self._pending_messages: dict[int, IncomingMessage] = {}
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._running = False
        self._mode_mgr = ModeManager()
        self._mechanic: MechanicAgent | None = None
        self._mechanic_report: str | None = None
        self._mechanic_task: asyncio.Task | None = None
        self._web_admin_agent: GeneralistAgent | None = None
        self._usage_state = None
        self._usage_near_limit_notified = False
        self._stop_called = False
        self._user_backgrounds: dict[int, str] = {}

    # ──────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._mode_mgr.transition(DaemonMode.STARTUP)

        # Logging with graceful fallback before config loads
        _setup_logging()

        # Web UI starts FIRST — available in all modes
        _set_web_daemon(self)
        try:
            web_cfg = config.section("web") if config._settings else {}
        except Exception:
            web_cfg = {}
        uvicorn_config = uvicorn.Config(
            web_app,
            host=web_cfg.get("host", "0.0.0.0"),
            port=web_cfg.get("port", 8080),
            log_config=_uvicorn_log_config(),
            loop="none",
        )
        self._web_server = uvicorn.Server(uvicorn_config)
        asyncio.create_task(self._web_server.serve(), name="web-server")

        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

        # Mode detection (also loads config as side effect if successful)
        mode, reason = await detect_startup_mode()
        self._mode_mgr.transition(mode, reason)
        logger.info("Startup mode: %s%s", mode.value, f" — {reason}" if reason else "")

        if mode == DaemonMode.INITIALIZE:
            setup_token = secrets.token_hex(16)
            _set_web_setup_token(setup_token)
            # Print to stdout only — never to the log file (which is served via /api/logs)
            print("=" * 60, flush=True)
            print(f"SETUP TOKEN: {setup_token}", flush=True)
            print("Open the web UI and enter this token to configure.", flush=True)
            print("=" * 60, flush=True)
            logger.info("Daemon in INITIALIZE mode — setup token printed to stdout")
            self._running = True
            asyncio.create_task(self._init_poll_loop(), name="init-poll")
            return

        if mode == DaemonMode.MIGRATE:
            logger.info("MIGRATE: spawning Mechanic for schema migration")
            self._running = True
            await self._spawn_mechanic(reason or "Schema migration required", MechanicContext.MIGRATE)
            return

        # RUN mode — normal startup
        await self._init_run_components()

    async def _init_run_components(self) -> None:
        """Initialize all runtime components. Called in RUN mode."""
        self._mode_mgr.transition(DaemonMode.RUN)

        self._conn = await db.init()
        tg_cfg = config.section("telegram")
        self._api = TelegramAPI(tg_cfg["token"])

        self._board = KanbanBoard(self._conn)
        self._token_tracker = TokenTracker(self._conn)

        self._coordinator = AgentCoordinator(
            board=self._board,
            token_tracker=self._token_tracker,
            on_result=self._on_agent_result,
            on_requeue=self._on_task_requeued,
            user_backgrounds=self._user_backgrounds,
        )
        self._coordinator.start()

        self._poller = TelegramPoller(self._api, self._on_update)
        self._poller.start()

        cfg = config.section("users")
        tg_cfg = config.section("telegram")
        admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))
        self._security.configure(self._api.send_message, admin_ids)

        asyncio.create_task(self._config_watcher_loop(), name="config-watcher")
        asyncio.create_task(self._usage_poll_loop(), name="usage-poller")

        self._running = True
        logger.info("claude-works daemon started in RUN mode")

    async def stop(self) -> None:
        if self._stop_called:
            return
        self._stop_called = True
        self._running = False
        if self._mechanic_task and not self._mechanic_task.done():
            self._mechanic_task.cancel()
            try:
                await self._mechanic_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._web_server:
            self._web_server.should_exit = True
        if self._poller:
            await self._poller.stop()
        if self._coordinator:
            await self._coordinator.stop()
        for t in self._typing_tasks.values():
            t.cancel()
        if self._api:
            await self._api.close()
        if self._conn:
            await self._conn.close()
        if os.path.exists(PID_FILE):
            os.unlink(PID_FILE)
        logger.info("claude-works daemon stopped")

    async def run_forever(self) -> None:
        await self.start()
        try:
            while self._running:
                await asyncio.sleep(1.0)
        finally:
            await self.stop()

    # ──────────────────────────────────────────────────────────
    # Mode management
    # ──────────────────────────────────────────────────────────

    @property
    def mode(self) -> DaemonMode:
        return self._mode_mgr.mode

    def health(self) -> dict:
        h: dict = {
            "status": "ok" if self._running else "stopped",
            "mode": self._mode_mgr.mode.value,
            "poller": self._poller.is_running if self._poller else False,
            "active_agents": self._coordinator.active_count if self._coordinator else 0,
            "security_pending": self._security.pending_count,
        }
        if self._mode_mgr.error:
            h["mode_error"] = self._mode_mgr.error
        if self._mechanic_report:
            h["mechanic_report"] = self._mechanic_report
        if self._coordinator and self._coordinator.is_rate_limited:
            h["rate_limited_until"] = self._coordinator.rate_limit_until
        if self._usage_state is not None:
            h["llm_usage"] = self._usage_state.as_dict()
        return h

    async def _init_poll_loop(self) -> None:
        """Poll for configuration in INITIALIZE mode. Transitions to RUN when ready."""
        while self._running and self._mode_mgr.mode == DaemonMode.INITIALIZE:
            await asyncio.sleep(10.0)
            mode, reason = await detect_startup_mode()
            if mode == DaemonMode.RUN:
                logger.info("Configuration ready — transitioning to RUN mode")
                await self._init_run_components()
                return
            if mode == DaemonMode.MIGRATE:
                self._mode_mgr.transition(DaemonMode.MIGRATE, reason)
                await self._spawn_mechanic(reason or "Migration required", MechanicContext.MIGRATE)
                return

    async def _spawn_mechanic(self, context: str, mech_mode: MechanicContext) -> None:
        """Spawn MechanicAgent. Handles both MIGRATE and REPAIR modes."""
        from .llm.provider import get_provider

        self._mode_mgr.transition(
            DaemonMode.MIGRATE if mech_mode == MechanicContext.MIGRATE else DaemonMode.REPAIR,
            context,
        )

        try:
            provider = get_provider(config.section("llm"))
        except Exception:
            provider = None

        self._mechanic = MechanicAgent(
            context=context,
            mode=mech_mode,
            provider=provider,
        )
        self._mechanic_task = asyncio.create_task(
            self._mechanic_loop(), name=f"mechanic-{mech_mode.value}"
        )

    async def _mechanic_loop(self) -> None:
        """Run MechanicAgent and notify admins of result."""
        if not self._mechanic:
            return
        try:
            report = await self._mechanic.run_initial()
            self._mechanic_report = report
            logger.info("Mechanic report ready (%d chars)", len(report))
            await self._notify_admins_mechanic(report)
        except Exception as exc:
            logger.exception("Mechanic loop failed: %s", exc)
            self._mechanic_report = f"Mechanic failed: {exc}"

    async def _notify_admins_mechanic(self, report: str) -> None:
        if not self._api:
            return
        cfg = config.section("users")
        tg_cfg = config.section("telegram")
        admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))
        mode_label = self._mode_mgr.mode.value.upper()
        msg = f"[{mode_label}] Mechanic report:\n\n{report[:4000]}"
        for admin_id in admin_ids:
            try:
                await self._api.send_message(admin_id, msg)
            except Exception:
                pass

    async def trigger_repair(self, error: str) -> None:
        """Enter REPAIR mode and spawn MechanicAgent."""
        if self._mode_mgr.mode == DaemonMode.REPAIR:
            logger.warning("trigger_repair called while already in REPAIR mode — ignored")
            return
        if self._coordinator:
            await self._coordinator.stop()
            self._coordinator = None
        if self._mechanic_task and not self._mechanic_task.done():
            self._mechanic_task.cancel()
            try:
                await self._mechanic_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._spawn_mechanic(error, MechanicContext.REPAIR)

    async def exit_repair(self) -> None:
        """Exit REPAIR/MIGRATE mode and restart normal operation."""
        if self._mechanic_task and not self._mechanic_task.done():
            self._mechanic_task.cancel()
            try:
                await self._mechanic_task
            except (asyncio.CancelledError, Exception):
                pass
        self._mechanic = None
        self._mechanic_report = None
        self._mechanic_task = None
        await self._init_run_components()

    async def web_admin_chat(self, message: str) -> str:
        """Process admin message from web UI, return reply. Maintains multi-turn context."""
        if self._web_admin_agent is None:
            persona = ""
            if self._coordinator and self._coordinator._chief:
                persona = self._coordinator._chief.persona
            self._web_admin_agent = GeneralistAgent(
                task_id=0,
                user_context={"user_id": -1, "chat_id": -1, "caveman_mode": False},
                agent_class="chief",
                persona=persona,
            )
        now = int(time.time())
        await self._conn.execute(
            "INSERT INTO admin_chat_messages (role, content, sent_at) VALUES (?, ?, ?)",
            ("user", message, now),
        )
        await self._conn.commit()
        reply = await self._web_admin_agent.run(message)
        await self._conn.execute(
            "INSERT INTO admin_chat_messages (role, content, sent_at) VALUES (?, ?, ?)",
            ("assistant", reply, int(time.time())),
        )
        await self._conn.commit()
        return reply

    async def web_admin_history(self, limit: int = 100) -> list[dict]:
        """Return last N admin chat messages in chronological order."""
        async with self._conn.execute(
            "SELECT role, content, sent_at FROM admin_chat_messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["content"], "sent_at": r["sent_at"]} for r in reversed(rows)]

    # ──────────────────────────────────────────────────────────
    # Telegram handling
    # ──────────────────────────────────────────────────────────

    async def _on_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback_query(update["callback_query"])
            return
        if "message" in update or "edited_message" in update:
            msg_data = update.get("message") or update.get("edited_message")
            is_edited = "edited_message" in update
            await self._handle_message(msg_data, is_edited=is_edited)
        elif "message_reaction" in update:
            await self._handle_reaction(update["message_reaction"])

    async def _handle_message(self, msg: dict, is_edited: bool = False) -> None:
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
        user = await upsert_user(self._conn, telegram_id, name)

        if user.get("metadata"):
            import json as _json
            try:
                meta = _json.loads(user["metadata"]) if isinstance(user["metadata"], str) else user["metadata"]
                background = meta.get("background", "") if isinstance(meta, dict) else ""
            except Exception:
                background = ""
            if background:
                self._user_backgrounds[telegram_id] = background

        if not await is_allowed(self._conn, telegram_id):
            if user["role"] == "blocked":
                await self._notify_admin_new_user(telegram_id, name)
            return

        if text and text.startswith("/"):
            logger.info("Command %r from user=%d chat=%d", text.split()[0], telegram_id, chat_id)
            await self._handle_command(text, telegram_id, chat_id)
            return

        # In REPAIR/MIGRATE mode, route messages to Mechanic if admin
        if self._mode_mgr.mode in (DaemonMode.REPAIR, DaemonMode.MIGRATE):
            if await is_admin(self._conn, telegram_id) and self._mechanic and text:
                reply = await self._mechanic.followup(text)
                await self._api.send_message(chat_id, reply[:4096])
                return
            await self._api.send_message(
                chat_id,
                f"System in {self._mode_mgr.mode.value} mode. Please wait.",
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

        await self._conn.execute(
            """INSERT OR IGNORE INTO messages (telegram_message_id, chat_id, from_user_id, text, voice_file_id, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (incoming.telegram_message_id, incoming.chat_id, incoming.from_user_id,
             incoming.text, incoming.voice_file_id, incoming.timestamp),
        )
        await self._conn.commit()

        await self._api.set_message_reaction(chat_id, msg["message_id"], "👀")

        pending = self._pending_messages.get(chat_id)
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

        self._pending_messages[chat_id] = incoming

        await asyncio.sleep(2.0)
        if self._pending_messages.get(chat_id) is not incoming:
            return

        del self._pending_messages[chat_id]
        content = incoming.text or ""
        if incoming.voice_file_id:
            try:
                audio_bytes = await self._api.get_file(incoming.voice_file_id)
                cfg = config.section("transcription")
                api_key = cfg.get("openai_api_key", "")
                transcript = await _transcribe_audio(api_key, audio_bytes)
                if transcript:
                    content = transcript + ("\n" + content if content else "")
                else:
                    content = "[Voice message — transcription unavailable]" + ("\n" + content if content else "")
            except Exception as e:
                logger.warning("Voice download/transcription error: %s", e)
                content = "[Voice message — transcription failed]" + ("\n" + content if content else "")

        task = KanbanTask(
            id=None,
            chat_id=chat_id,
            user_id=telegram_id,
            content=content,
            priority=1 if content.startswith("!") else 0,
        )
        await self._board.push(task)
        self._start_typing(chat_id)

    async def _handle_reaction(self, reaction_data: dict) -> None:
        chat_id = reaction_data.get("chat", {}).get("id")
        message_id = reaction_data.get("message_id")
        from_user = reaction_data.get("user", {})
        new_reaction = reaction_data.get("new_reaction", [])

        emoji = extract_reaction_emoji(new_reaction)
        if not emoji or not chat_id:
            return

        tg_cfg = config.section("telegram")
        action = resolve_action(emoji, tg_cfg.get("reaction_map"))

        await self._conn.execute(
            """INSERT INTO reactions (telegram_message_id, chat_id, from_user_id, emoji, action, received_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (message_id, chat_id, from_user.get("id", 0), emoji, action, int(time.time())),
        )
        await self._conn.commit()
        logger.info("Reaction %s (%s) on message %s", emoji, action, message_id)

    async def _handle_callback_query(self, cq: dict) -> None:
        callback_query_id = cq.get("id", "")
        from_user = cq.get("from", {})
        chat = cq.get("message", {}).get("chat", {})
        chat_id = chat.get("id") or from_user.get("id")
        telegram_id = from_user.get("id")
        data = cq.get("data", "")
        if not telegram_id or not chat_id or not data:
            return
        await self._api.answer_callback_query(callback_query_id)
        fake_msg = {
            "message_id": cq.get("message", {}).get("message_id", 0),
            "chat": {"id": chat_id},
            "from": from_user,
            "text": data,
            "date": int(time.time()),
        }
        await self._handle_message(fake_msg)

    async def _handle_command(self, text: str, from_id: int, chat_id: int) -> None:
        parts = text.strip().split()
        cmd = parts[0].lower()

        if cmd == "/auth" and len(parts) >= 2:
            if not await is_admin(self._conn, from_id):
                await self._api.send_message(chat_id, "Nope.")
                return
            try:
                target_id = int(parts[1].lstrip("@"))
                await set_role(self._conn, target_id, "user")
                await self._api.send_message(chat_id, f"User {target_id} approved.")
            except Exception as e:
                await self._api.send_message(chat_id, f"Error: {e}")

        elif cmd == "/block" and len(parts) >= 2:
            if not await is_admin(self._conn, from_id):
                return
            try:
                target_id = int(parts[1].lstrip("@"))
                await set_role(self._conn, target_id, "blocked")
                await self._api.send_message(chat_id, f"User {target_id} blocked.")
            except Exception as e:
                await self._api.send_message(chat_id, f"Error: {e}")

        elif cmd == "/approve" and len(parts) >= 2:
            if not await is_admin(self._conn, from_id):
                return
            try:
                ok = self._security.approve(int(parts[1]), from_id)
                await self._api.send_message(chat_id, f"✓ Approved #{parts[1]}" if ok else f"No pending approval #{parts[1]}")
            except Exception as e:
                await self._api.send_message(chat_id, f"Error: {e}")

        elif cmd == "/deny" and len(parts) >= 2:
            if not await is_admin(self._conn, from_id):
                return
            try:
                ok = self._security.deny(int(parts[1]), from_id)
                await self._api.send_message(chat_id, f"✗ Denied #{parts[1]}" if ok else f"No pending approval #{parts[1]}")
            except Exception as e:
                await self._api.send_message(chat_id, f"Error: {e}")

        elif cmd == "/status":
            h = self.health()
            mode_info = f" | mode: {h['mode']}"
            sec = f" | sec: {h['security_pending']} pending" if h.get('security_pending') else ""
            msg = f"poller: {'✓' if h['poller'] else '✗'} | agents: {h['active_agents']} active{mode_info}{sec}"
            await self._api.send_message(chat_id, msg)

        elif cmd == "/reload_persona":
            if not await is_admin(self._conn, from_id):
                return
            if self._coordinator and self._coordinator._chief:
                self._coordinator._chief.reload_persona()
                await self._api.send_message(chat_id, "Persona reloaded.")
            else:
                await self._api.send_message(chat_id, "Chief not running.")

        elif cmd == "/reload_config":
            if not await is_admin(self._conn, from_id):
                return
            try:
                from .config_store import load_config as _load_db_cfg
                conn = await db.init_config()
                cfg = await _load_db_cfg(conn)
                row = None
                if cfg:
                    async with conn.execute(
                        "SELECT updated_at FROM daemon_config WHERE id=1"
                    ) as cur:
                        row = await cur.fetchone()
                await conn.close()
                if cfg:
                    config.set(cfg)
                    if row:
                        config._config_updated_at = row["updated_at"]
                    await self._api.send_message(chat_id, "Config reloaded from DB.")
                    logger.info("Config reloaded via /reload_config by user=%d", from_id)
                else:
                    await self._api.send_message(chat_id, "No config found in DB.")
            except Exception as e:
                await self._api.send_message(chat_id, f"Reload failed: {e}")

        elif cmd == "/repair" and len(parts) >= 2:
            if not await is_admin(self._conn, from_id):
                return
            error = " ".join(parts[1:])
            await self.trigger_repair(error)
            await self._api.send_message(chat_id, "Repair mode activated. Mechanic spawned.")

        elif cmd == "/exit_repair":
            if not await is_admin(self._conn, from_id):
                return
            if self._mode_mgr.mode not in (DaemonMode.REPAIR, DaemonMode.MIGRATE):
                await self._api.send_message(chat_id, "Not in repair/migrate mode.")
                return
            await self.exit_repair()
            await self._api.send_message(chat_id, "Exited repair mode. Normal operation resumed.")

    async def _on_agent_result(self, task: KanbanTask, result: str | None, error: str | None = None) -> None:
        self._stop_typing(task.chat_id)
        if result:
            allowed = await self._security.check(
                result, task_id=task.id, chat_id=task.chat_id, user_id=task.user_id
            )
            if not allowed:
                await self._api.send_message(task.chat_id, "Response blocked by security policy.")
                return
            clean_result, keyboard = _parse_buttons(result)
            clean_result, tts_text = _extract_voice_tag(clean_result)
            clean_result, map_query = _extract_map_tag(clean_result)
            reply_markup = {"inline_keyboard": keyboard} if keyboard is not None else None

            if clean_result.strip():
                html_result = _md_to_telegram_html(clean_result)
                try:
                    sent = await self._api.send_message(task.chat_id, html_result, parse_mode="HTML", reply_markup=reply_markup)
                except Exception:
                    logger.warning("HTML send failed for task=%d, retrying plain", task.id)
                    sent = await self._api.send_message(task.chat_id, clean_result, reply_markup=reply_markup)
            else:
                sent = {"message_id": 0}

            if tts_text:
                try:
                    from gtts import gTTS
                    import io
                    tts = gTTS(text=tts_text, lang="de")
                    buf = io.BytesIO()
                    tts.write_to_fp(buf)
                    buf.seek(0)
                    await self._api.send_voice(task.chat_id, buf.read())
                except Exception as e:
                    logger.warning("TTS failed for task=%d: %s", task.id, e)

            if map_query:
                try:
                    async with __import__("httpx").AsyncClient(timeout=10.0) as hc:
                        r = await hc.get(
                            "https://nominatim.openstreetmap.org/search",
                            params={"q": map_query, "format": "json", "limit": 1},
                            headers={"User-Agent": "claude-works-bot/1.0"},
                        )
                        results = r.json()
                    if results:
                        lat = float(results[0]["lat"])
                        lon = float(results[0]["lon"])
                        title = results[0].get("display_name", map_query)[:60]
                        await self._api.send_location(task.chat_id, lat, lon, title=title)
                    else:
                        await self._api.send_message(task.chat_id, f"📍 {map_query} — nicht gefunden.")
                except Exception as e:
                    logger.warning("Map geocoding failed for task=%d: %s", task.id, e)

            await self._conn.execute(
                """INSERT INTO bot_messages (telegram_message_id, chat_id, task_id, text, sent_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (sent["message_id"], task.chat_id, task.id, clean_result, int(time.time())),
            )
            await self._conn.commit()
        elif error:
            await self._api.send_message(task.chat_id, f"Error: {error}")

    async def _on_task_requeued(self, task: KanbanTask) -> None:
        self._stop_typing(task.chat_id)

    async def _notify_admin_new_user(self, telegram_id: int, name: str | None) -> None:
        cfg = config.section("users")
        tg_cfg = config.section("telegram")
        admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))
        for admin_id in admin_ids:
            await self._api.send_message(
                admin_id,
                f"New user requesting access: {name or 'unknown'} (ID: {telegram_id})\n/auth {telegram_id}",
            )

    def _start_typing(self, chat_id: int) -> None:
        if chat_id in self._typing_tasks:
            return
        self._typing_tasks[chat_id] = asyncio.create_task(
            self._typing_loop(chat_id),
            name=f"typing-{chat_id}",
        )

    def _stop_typing(self, chat_id: int) -> None:
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()

    async def _typing_loop(self, chat_id: int) -> None:
        try:
            while True:
                await self._api.send_chat_action(chat_id, "typing")
                await asyncio.sleep(TYPING_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _config_watcher_loop(self) -> None:
        """Poll config.db every 5s; reload in-memory config when updated_at changes."""
        from .config_store import load_config as _load_db_config
        try:
            while self._running:
                await asyncio.sleep(5.0)
                try:
                    conn = await db.init_config()
                    async with conn.execute(
                        "SELECT updated_at FROM daemon_config WHERE id=1"
                    ) as cur:
                        row = await cur.fetchone()
                    if row and row["updated_at"] != config._config_updated_at:
                        cfg = await _load_db_config(conn)
                        if cfg:
                            config.set(cfg)
                            config._config_updated_at = row["updated_at"]
                            logger.info("Config reloaded from DB")
                    await conn.close()
                except Exception as exc:
                    logger.debug("Config watcher error: %s", exc)
        except asyncio.CancelledError:
            pass

    async def _usage_poll_loop(self) -> None:
        try:
            while self._running:
                interval = config.section("llm").get("usage_poll_interval_seconds", 300)
                if interval <= 0:
                    await asyncio.sleep(60.0)
                    continue
                await asyncio.sleep(interval)
                if not self._coordinator:
                    continue
                stats = await self._coordinator.query_usage()
                if stats is None:
                    continue
                self._usage_state = stats
                pct_str = f"{stats.usage_pct * 100:.0f}%" if stats.usage_pct is not None else "?"
                logger.info("Claude Code usage: %s (tokens %s/%s)", pct_str, stats.tokens_used, stats.tokens_limit)
                if stats.is_near_limit and not self._usage_near_limit_notified:
                    self._usage_near_limit_notified = True
                    await self._notify_admins_usage(stats)
                elif not stats.is_near_limit:
                    self._usage_near_limit_notified = False
        except asyncio.CancelledError:
            pass

    async def _notify_admins_usage(self, stats) -> None:
        if not self._api:
            return
        cfg = config.section("users")
        tg_cfg = config.section("telegram")
        admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))
        pct = f"{stats.usage_pct * 100:.0f}%" if stats.usage_pct is not None else "high"
        reset_str = ""
        if stats.reset_in_seconds:
            h, m = divmod(stats.reset_in_seconds // 60, 60)
            reset_str = f" | resets in {h}h {m}m"
        msg = f"[USAGE] Claude Code usage at {pct}{reset_str}"
        for admin_id in admin_ids:
            try:
                await self._api.send_message(admin_id, msg)
            except Exception:
                pass


async def run() -> None:
    _setup_logging()
    daemon = Daemon()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.stop()))

    await daemon.run_forever()


if __name__ == "__main__":
    asyncio.run(run())
