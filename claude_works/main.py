import asyncio
import hashlib
import json
import logging
import re
import urllib.parse
from .tasks.transcriber import transcribe as _transcribe_audio
from .tasks.tts import synthesize as _synthesize_tts
from .tasks.email import send_email as _send_email, read_emails as _read_emails
from .tasks.github import github_api as _github_api
import secrets
import time
import os
import signal
from typing import Any

import httpx
import uvicorn
from datetime import datetime, timezone as _UTC

from . import config, db
from .fetcher import fetch_url_content as _fetch_url_content
from .config_store import load_config as _load_db_config, save_config as _save_db_config
from .mode import DaemonMode, ModeManager, detect_startup_mode
from .telegram.api import TelegramAPI
from .telegram.poller import TelegramPoller
from .telegram.reactions import resolve_action, extract_reaction_emoji
from .tasks.models import IncomingMessage
from .tasks.bundler import should_bundle, merge_content
from .kanban.board import KanbanBoard, build_offload_content, is_offloaded
from .kanban.models import AgentClass, KanbanTask
from .telemetry.tokens import TokenTracker
from .knowledge import store as knowledge_store
from .agents.coordinator import AgentCoordinator
from .agents.heartbeat import run_with_heartbeat
from .llm.errors import RateLimitError
from .agents.mechanic import MechanicAgent, MechanicContext
from .agents.specialist.generalist import GeneralistAgent
from .tasks import tags as _tags
from .tasks.tags import collect_output_tags as _collect_output_tags, TagCollection as _TagCollection
from .tasks.tor import restart_tor as _restart_tor
from .tasks.executor import exec_tool_tags as _exec_tool_tags_fn
from .kanban.models import Lane as _Lane
from .llm.provider import get_provider as _get_provider
from .telegram.renderer import md_to_html as _md_to_telegram_html
from .tasks.reminders import (
    parse_remind_at as _parse_remind_at,
    add_reminder as _add_reminder,
    list_reminders as _list_reminders,
    delete_reminder as _delete_reminder,
    fire_due_reminders as _fire_due_reminders,
)
from .auth.users import upsert_user, is_allowed, is_admin, set_role, set_trust
from .auth import trust as trust_mod
from .security import SecuritySupervisor
from .security import whitelist as _whitelist
from .web.app import app as web_app, set_daemon as _set_web_daemon, set_setup_token as _set_web_setup_token
from .logging_setup import setup as _setup_logging, uvicorn_log_config as _uvicorn_log_config

logger = logging.getLogger(__name__)

TYPING_INTERVAL = 4.0
PID_FILE = "/data/claude-works.pid"



# TAG extractors — all logic lives in tasks/tags.py
_extract_voice_tag = _tags.extract_voice
_extract_map_tag = _tags.extract_map
_parse_buttons = _tags.parse_buttons
_extract_send_email_tag = _tags.extract_send_email
_extract_read_email_tag = _tags.extract_read_email
_extract_github_api_tag = _tags.extract_github_api
_extract_git_clone_tag = _tags.extract_git_clone
_extract_mute_tag = _tags.extract_mute
_extract_unmute_tag = _tags.extract_unmute
_extract_get_config_tag = _tags.extract_get_config
_extract_shell_tag = _tags.extract_shell
_extract_board_task_tag = _tags.extract_board_task
_extract_orchestrate_tag = _tags.extract_orchestrate
_extract_kb_search_tag = _tags.extract_kb_search
_extract_kb_save_tag = _tags.extract_kb_save
_extract_kb_update_tag = _tags.extract_kb_update
_extract_config_update_tag = _tags.extract_config_update
_extract_plugin_config_get_tag = _tags.extract_plugin_config_get
_extract_plugin_config_set_tag = _tags.extract_plugin_config_set
_kb_write_allowed = _tags.kb_write_allowed
_CONFIG_UPDATE_BLOCKED = _tags.CONFIG_UPDATE_BLOCKED
_get_config_by_dotpath = _tags.get_config_by_dotpath
_extract_remind_tag = _tags.extract_remind


_LONG_RUN_NOTICE_SECONDS = 60.0  # one-shot "still working" notice for inline chat runs

_PLUGINS_DIR = _tags.PLUGINS_DIR
_URL_RE = re.compile(r'https?://[^\s<>"\']+')
_MAX_FETCH_URLS = 3

_MAX_TOOL_OUTPUT_CHARS = 4000
_MIN_ECHO_LINE_CHARS = 24
_STRUCTURAL_LINE_RE = re.compile(r'^(?:[{}\[\],]+|"[\w-]+":.*)$')
_MAX_TRACKED_PAYLOADS = 24
_TOR_SOCKS_DEFAULT = "socks5://127.0.0.1:9050"

_build_git_clone_cmd = _tags.build_git_clone_cmd
_extract_tor_restart_tag = _tags.extract_tor_restart

_logger = logging.getLogger(__name__)

# ── User-facing error abstraction ─────────────────────────────────────────────

def _user_error(context: str, exc: Exception | None = None) -> str:
    """Return a user-friendly error string. Logs the full exception internally.

    Never exposes raw exception messages, stack traces, or internal details to
    the user. context describes WHAT failed; exc is logged but not shown.
    """
    if exc is not None:
        _logger.warning("%s: %s", context, exc)
    _FRIENDLY: dict[type, str] = {
        asyncio.TimeoutError: "Zeitüberschreitung.",
    }
    if exc is not None:
        for exc_type, msg in _FRIENDLY.items():
            if isinstance(exc, exc_type):
                return f"⚠️ {context} — {msg}"
    return f"⚠️ {context}."


# _restart_tor moved to tasks/tor.py; imported at top as _restart_tor


_TASK_VERB_RE = re.compile(
    r'\b(schreib|erstell|generier|entwickel|implementier|analysier|recherchier|'
    r'migrier|repari|konvertier|deploy|extrahier|zusammenfass|berechne?|kalkulier|'
    r'write|create|build|develop|implement|generate|analyse|analyze|research|'
    r'migrate|repair|convert|extract|summarize|calculate|deploy)\b',
    re.IGNORECASE,
)
_TASK_MAX_CHAT_LEN = 400


def _is_task(content: str) -> bool:
    """Returns True if content looks like work to track in kanban, not plain conversation."""
    stripped = content.strip()
    if len(stripped) > _TASK_MAX_CHAT_LEN:
        return True
    if _TASK_VERB_RE.search(stripped):
        return True
    return False


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
        self._chat_task_ids: set[int] = set()
        self._chat_reply_to: dict[int, int] = {}
        self._active_chat_content: dict[int, str] = {}
        self._pending_chat_queue: dict[int, list] = {}
        self._chat_task_start_times: dict[int, float] = {}
        self._chat_exception_count: int = 0
        self._running = False
        self._mode_mgr = ModeManager()
        self._cron: Any = None  # CronManager — durable scheduled jobs
        self._mechanic: MechanicAgent | None = None
        self._mechanic_report: str | None = None
        self._mechanic_task: asyncio.Task | None = None
        self._web_admin_agent: GeneralistAgent | None = None
        self._usage_state = None
        self._usage_near_limit_notified = False
        self._stop_called = False
        self._user_backgrounds: dict[int, str] = {}
        self._user_personas: dict[int, str] = {}
        self._pending_direct_fetches: dict[str, dict] = {}
        self._pending_reauth: dict[int, asyncio.subprocess.Process] = {}  # chat_id → proc
        self._chat_agents: dict[int, GeneralistAgent] = {}  # chat_id → persistent chat agent
        # chat_id → fingerprint of the config-derived inputs baked into that agent's
        # system prompt. On config reload we drop only agents whose fingerprint changed,
        # instead of nuking every warm conversation project-wide.
        self._chat_agent_fingerprints: dict[int, tuple] = {}
        # chat_id → tool-output / fetched payloads handed to the agent this turn.
        # Used by _strip_echoed_payloads to remove verbatim echoes before sending.
        self._recent_tool_payloads: dict[int, list[str]] = {}
        self._bot_username: str = ""  # loaded at startup via getMe
        self._bot_id: int = 0  # loaded at startup via getMe
        self._mention_only_chats: set[int] = set()  # chat_ids where bot only responds to @mention
        self._muted_users: dict[int, int] = {}  # telegram_id → muted-until epoch (0 = indefinite)
        self._group_reply_log: dict[tuple[int, int], list[float]] = {}  # (chat_id, user_id) → bot-reply timestamps
        self._exchange_depth: dict[int, tuple[int, int]] = {}  # chat_id → (user_id, consecutive bot replies to that user)
        self._pending_reactions: dict[int, tuple[int, int]] = {}  # kanban task_id → (chat_id, tg_msg_id)
        self._pending_initial_msgs: dict[int, int] = {}  # kanban task_id → preliminary tg_msg_id

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
        from .startup import init_run_components
        await init_run_components(self)

    async def _reset_stale_tasks(self) -> None:
        from .startup import reset_stale_tasks
        await reset_stale_tasks(self)

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

        self._mode_mgr.transition(
            DaemonMode.MIGRATE if mech_mode == MechanicContext.MIGRATE else DaemonMode.REPAIR,
            context,
        )

        try:
            provider = _get_provider(config.section("llm"))
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
        """Enter REPAIR mode and spawn MechanicAgent. Notifies admins."""
        if self._mode_mgr.mode == DaemonMode.REPAIR:
            logger.warning("trigger_repair called while already in REPAIR mode — ignored")
            return
        logger.error("Entering REPAIR mode: %s", error)
        # Notify admins before stopping coordinator
        cfg = config.section("users")
        tg_cfg = config.section("telegram")
        admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))
        if self._api and admin_ids:
            msg = f"⚠️ REPAIR MODE\n\nGrund: {error[:300]}\n\nMechanic analysiert das Problem."
            for admin_id in admin_ids:
                try:
                    await self._api.send_message(admin_id, msg)
                except Exception:
                    pass
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

    async def _build_status_snapshot(self) -> str:
        from .web.admin_chat import build_status_snapshot
        return await build_status_snapshot(self)

    async def web_admin_chat(self, message: str) -> dict:
        from .web.admin_chat import web_admin_chat
        return await web_admin_chat(self, message)

    async def web_admin_history(self, limit: int = 100) -> list[dict]:
        from .web.admin_chat import web_admin_history
        return await web_admin_history(self, limit)

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
        from .telegram.message_handler import handle_message
        await handle_message(self, msg, is_edited)

    async def _enrich_voice(self, voice_file_id: str, existing_text: str) -> str:
        """Download and transcribe a voice message. Returns enriched content string."""
        try:
            audio_bytes = await self._api.get_file(voice_file_id)
            api_key = config.section("tts").get("elevenlabs_api_key", "")
            transcript = await _transcribe_audio(api_key, audio_bytes)
            if transcript:
                return transcript + ("\n" + existing_text if existing_text else "")
            return "[Voice message — transcription unavailable]" + ("\n" + existing_text if existing_text else "")
        except Exception as e:
            logger.warning("Voice download/transcription error: %s", e)
            return "[Voice message — transcription failed]" + ("\n" + existing_text if existing_text else "")

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

        # Expire old pending fetches
        now = time.time()
        self._pending_direct_fetches = {
            k: v for k, v in self._pending_direct_fetches.items() if v["expires_at"] > now
        }

        if data.startswith("direct:") or data.startswith("deny:"):
            await self._api.answer_callback_query(callback_query_id)
            fetch_hash = data.split(":", 1)[1]
            pending = self._pending_direct_fetches.pop(fetch_hash, None)
            if not pending or time.time() > pending["expires_at"]:
                await self._api.send_message(chat_id, "Request expired.")
                return
            content = pending["content"]
            if data.startswith("direct:"):
                for url in pending["urls"]:
                    page_text = await _fetch_url_content(url)  # direct, no proxy
                    if page_text:
                        content = content + f"\n\n---\n\n[Content of {url}]\n{page_text}"
                        self._track_payloads(chat_id, [page_text])
                        logger.info("Direct URL fetch approved by user: %s", url)
            task = KanbanTask(
                id=None,
                chat_id=pending["chat_id"],
                user_id=pending["user_id"],
                content=content,
                priority=0,
            )
            await self._board.push(task)
            self._start_typing(pending["chat_id"])
            return

        if data.startswith("sec_"):
            await self._api.answer_callback_query(callback_query_id)
            scope, _, rest = data.partition(":")
            try:
                approval_id = int(rest)
            except ValueError:
                return
            if scope == "sec_once":
                ok = self._security.approve(approval_id, telegram_id)
                reply = "✅ Approved (once)."
            elif scope == "sec_deny":
                ok = self._security.deny(approval_id, telegram_id)
                reply = "❌ Denied."
            elif scope == "sec_always_specific":
                ok = self._security.approve_always_specific(approval_id, telegram_id)
                reply = "🔁 Specific permission saved permanently."
            elif scope == "sec_always_action":
                ok = self._security.approve_always_action(approval_id, telegram_id)
                reply = "🔄 Action permanently approved — future requests of this type will be auto-approved."
            else:
                return
            if ok:
                sec_orig_msg = cq.get("message") or {}
                sec_orig_id = sec_orig_msg.get("message_id", 0)
                sec_orig_text = sec_orig_msg.get("text", "")
                sec_entities = sec_orig_msg.get("entities") or None
                if sec_orig_id and sec_orig_text:
                    try:
                        await self._api.edit_message(
                            chat_id, sec_orig_id,
                            f"{sec_orig_text}\n\n→ {reply}",
                            remove_keyboard=True,
                            entities=sec_entities,
                        )
                    except Exception:
                        await self._api.send_message(chat_id, reply)
                else:
                    await self._api.send_message(chat_id, reply)
            return

        if data.startswith("kb_approve:") or data.startswith("kb_reject:"):
            await self._api.answer_callback_query(callback_query_id)
            if not await is_admin(self._conn, telegram_id):
                logger.warning("KB quarantine callback from non-admin user=%d ignored", telegram_id)
                return
            try:
                entry_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            conn = await db.get_conn()
            if data.startswith("kb_approve:"):
                ok = await knowledge_store.approve(conn, entry_id)
                reply = f"✅ KB-Eintrag {entry_id} freigegeben." if ok else f"⚠ KB-Eintrag {entry_id} nicht gefunden."
            else:
                ok = await knowledge_store.delete(conn, entry_id)
                reply = f"🗑 KB-Eintrag {entry_id} gelöscht." if ok else f"⚠ KB-Eintrag {entry_id} nicht gefunden."
            await conn.close()
            kb_orig_msg = cq.get("message") or {}
            kb_orig_id = kb_orig_msg.get("message_id", 0)
            kb_orig_text = kb_orig_msg.get("text", "")
            kb_entities = kb_orig_msg.get("entities") or None
            if kb_orig_id and kb_orig_text:
                try:
                    await self._api.edit_message(
                        chat_id, kb_orig_id,
                        f"{kb_orig_text}\n\n→ {reply}",
                        remove_keyboard=True,
                        entities=kb_entities,
                    )
                except Exception:
                    await self._api.send_message(chat_id, reply)
            else:
                await self._api.send_message(chat_id, reply)
            return

        await self._api.answer_callback_query(callback_query_id)
        # Resolve button label from the original message's inline keyboard
        orig_msg = cq.get("message") or {}
        orig_msg_id = orig_msg.get("message_id", 0)
        orig_text = orig_msg.get("text", "")
        orig_entities = orig_msg.get("entities") or None
        keyboard = (orig_msg.get("reply_markup") or {}).get("inline_keyboard", [])
        btn_label = data
        for row in keyboard:
            for btn in row:
                if btn.get("callback_data") == data:
                    btn_label = btn.get("text", data)
                    break
        # Edit original message: append selection, remove keyboard
        if orig_msg_id and orig_text:
            try:
                await self._api.edit_message(
                    chat_id, orig_msg_id,
                    f"{orig_text}\n\n→ {btn_label}",
                    remove_keyboard=True,
                    entities=orig_entities,
                )
            except Exception:
                pass
        fake_msg = {
            "message_id": orig_msg_id,
            "chat": {"id": chat_id},
            "from": from_user,
            "text": data,
            "date": int(time.time()),
        }
        await self._handle_message(fake_msg)

    async def _handle_command(self, text: str, from_id: int, chat_id: int) -> None:
        from .telegram.commands import handle_command
        await handle_command(self, text, from_id, chat_id)

    async def _check_cli_auth_on_startup(self, admin_ids: list[int]) -> None:
        """Check CLI auth on startup and notify admins if not authenticated."""
        await asyncio.sleep(3.0)  # let poller settle first
        llm_cfg = config.section("llm")
        binary = llm_cfg.get("cli_binary") or "claude"
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "-p", "ping", "--output-format", "json",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            if proc.returncode != 0:
                try:
                    result_text = json.loads(stdout.decode()).get("result", "")
                except Exception:
                    result_text = ""
                if re.search(r"not logged in|login|auth", result_text, re.IGNORECASE):
                    for admin_id in admin_ids:
                        try:
                            await self._api.send_message(
                                admin_id,
                                "⚠️ Claude CLI nicht eingeloggt. Bitte /reauth ausführen."
                            )
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("Startup CLI auth check failed: %s", e)

    async def _start_telegram_reauth(self, chat_id: int) -> None:
        cfg = config.section("llm")
        binary = cfg.get("cli_binary") or "claude"
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "auth", "login",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            await self._api.send_message(chat_id, f"CLI binary not found: {binary}")
            return
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
            await self._api.send_message(chat_id, _user_error("Authentifizierung konnte nicht gestartet werden", exc))
            return
        if not url:
            await self._api.send_message(chat_id, f"No auth URL found. Output: {buf[:300]}")
            return
        self._pending_reauth[chat_id] = proc
        await self._api.send_message(
            chat_id,
            f"Open this URL in your browser, then send me the code:\n{url}"
        )

    async def _on_agent_result(self, task: KanbanTask, result: str | None, error: str | None = None) -> None:
        from .tasks.agent_result import on_agent_result
        await on_agent_result(self, task, result, error)

    async def _on_task_requeued(self, task: KanbanTask) -> None:
        if task.id in self._chat_task_ids:
            self._chat_task_ids.discard(task.id)
            self._stop_typing(task.chat_id)
            self._flush_chat_queue(task.chat_id)

    async def _load_mention_only_chats(self) -> None:
        try:
            async with self._conn.execute(
                "SELECT value FROM daemon_state WHERE key = 'mention_only_chats'"
            ) as cur:
                row = await cur.fetchone()
            if row:
                self._mention_only_chats = set(json.loads(row[0]))
                logger.info("Loaded %d mention-only chats", len(self._mention_only_chats))
        except Exception as e:
            logger.warning("Failed to load mention_only_chats: %s", e)

    async def _save_mention_only_chats(self) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO daemon_state (key, value, updated_at) VALUES (?, ?, ?)""",
            ("mention_only_chats", json.dumps(sorted(self._mention_only_chats)), int(time.time())),
        )
        await self._conn.commit()

    # ──────────────────────────────────────────────────────────
    # Hard mute + group guard (enforced in dispatch, not in prompt)
    # ──────────────────────────────────────────────────────────

    async def _load_muted_users(self) -> None:
        try:
            async with self._conn.execute(
                "SELECT value FROM daemon_state WHERE key = 'muted_users'"
            ) as cur:
                row = await cur.fetchone()
            if row:
                self._muted_users = {int(k): int(v) for k, v in json.loads(row[0]).items()}
                logger.info("Loaded %d muted users", len(self._muted_users))
        except Exception as e:
            logger.warning("Failed to load muted_users: %s", e)

    async def _save_muted_users(self) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO daemon_state (key, value, updated_at) VALUES (?, ?, ?)""",
            ("muted_users", json.dumps({str(k): v for k, v in self._muted_users.items()}), int(time.time())),
        )
        await self._conn.commit()

    def _is_muted(self, telegram_id: int) -> bool:
        until = self._muted_users.get(telegram_id)
        if until is None:
            return False
        if until != 0 and time.time() > until:
            # Expired — prune lazily; persistence happens on next explicit change
            del self._muted_users[telegram_id]
            logger.info("Mute expired for user=%d", telegram_id)
            return False
        return True

    async def _set_mute(self, telegram_id: int, minutes: int) -> int:
        """Mute user. minutes<=0 → indefinite. Returns until-epoch (0 = indefinite)."""
        until = 0 if minutes <= 0 else int(time.time()) + minutes * 60
        self._muted_users[telegram_id] = until
        await self._save_muted_users()
        logger.info("Muted user=%d until=%s", telegram_id, until or "indefinite")
        return until

    async def _clear_mute(self, telegram_id: int) -> bool:
        if telegram_id in self._muted_users:
            del self._muted_users[telegram_id]
            await self._save_muted_users()
            logger.info("Unmuted user=%d", telegram_id)
            return True
        return False

    async def _resolve_user(self, ident: str) -> "dict | None":
        """Resolve a user by telegram_id or (partial) name. Returns row dict or None."""
        ident = ident.strip().lstrip("@")
        try:
            tid = int(ident)
            async with self._conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (tid,)
            ) as cur:
                row = await cur.fetchone()
            return dict(row) if row else None
        except ValueError:
            pass
        async with self._conn.execute(
            "SELECT * FROM users WHERE LOWER(name) LIKE ? ORDER BY last_seen DESC LIMIT 1",
            (f"%{ident.lower()}%",),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    def _group_guard_allows(self, chat_id: int, user_id: int) -> bool:
        """Loop brake for group chats: sliding-window reply budget per user +
        max consecutive bot↔same-user exchanges. Admins are exempt (checked by caller)."""
        try:
            cfg = config.section("group_guard") or {}
        except Exception:
            cfg = {}
        max_replies = int(cfg.get("max_replies_per_window", 3))
        window = int(cfg.get("window_seconds", 600))
        max_depth = int(cfg.get("max_consecutive_replies", 4))
        now = time.time()
        key = (chat_id, user_id)
        recent = [t for t in self._group_reply_log.get(key, []) if now - t < window]
        self._group_reply_log[key] = recent
        if len(recent) >= max_replies:
            logger.info("Group guard: window budget hit (%d/%ds) chat=%d user=%d", max_replies, window, chat_id, user_id)
            return False
        depth_user, depth = self._exchange_depth.get(chat_id, (0, 0))
        if depth_user == user_id and depth >= max_depth:
            logger.info("Group guard: exchange depth %d hit chat=%d user=%d", max_depth, chat_id, user_id)
            return False
        return True

    def _record_group_reply(self, chat_id: int, user_id: int) -> None:
        self._group_reply_log.setdefault((chat_id, user_id), []).append(time.time())
        depth_user, depth = self._exchange_depth.get(chat_id, (0, 0))
        self._exchange_depth[chat_id] = (user_id, depth + 1 if depth_user == user_id else 1)

    async def _load_chat_history(self, chat_id: int, limit: int = 50) -> list[dict]:
        """Load recent conversation from DB as {role, content} pairs, chronological order.

        Only includes bot messages from direct-chat tasks (agent_id='chat') to prevent
        board task results from bleeding into the chat agent's context.
        """
        async with self._conn.execute(
            """SELECT 'user' AS role, text, timestamp AS ts
               FROM messages WHERE chat_id = ? AND text IS NOT NULL
               UNION ALL
               SELECT 'assistant' AS role, bm.text, bm.sent_at AS ts
               FROM bot_messages bm
               JOIN kanban_tasks kt ON bm.task_id = kt.id
               WHERE bm.chat_id = ? AND bm.text IS NOT NULL AND kt.agent_id = 'chat'
               ORDER BY ts DESC LIMIT ?""",
            (chat_id, chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["text"]} for r in reversed(rows)]

    async def _handle_chat(self, chat_id: int, user_id: int, content: str, reply_to_msg_id: int | None = None) -> None:
        from .telegram.chat_handler import handle_chat
        await handle_chat(self, chat_id, user_id, content, reply_to_msg_id)

    async def _long_run_notice(self, chat_id: int) -> None:
        """Send a single status message when an inline run exceeds the notice delay."""
        try:
            await asyncio.sleep(_LONG_RUN_NOTICE_SECONDS)
            await self._api.send_message(chat_id, "⏳ Dauert noch. Bin dran.")
            logger.info("Chat %d: long-run notice sent after %.0fs", chat_id, _LONG_RUN_NOTICE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _offload_after_timeout(
        self, chat_id: int, user_id: int, content: str, task_id: int | None, elapsed: float,
    ) -> None:
        from .telegram.chat_handler import offload_after_timeout
        await offload_after_timeout(self, chat_id, user_id, content, task_id, elapsed)

    def _chat_config_fingerprint(self, chat_id: int) -> tuple:
        """Config-derived inputs that determine a chat agent's system prompt.

        Per-user persona is runtime state (not config) so it's excluded — a config
        reload can't change it. Only the global chief persona and per-group
        persona/focus/style come from config and can shift on reload.
        """
        chief_persona = ""
        if self._coordinator and self._coordinator._chief:
            chief_persona = self._coordinator._chief.persona
        grp = config.group_config(chat_id)
        return (
            chief_persona,
            grp.get("persona", ""),
            grp.get("focus", ""),
            grp.get("communication_style", ""),
        )

    def _invalidate_stale_chat_agents(self) -> int:
        """Drop only chat agents whose config-derived prompt inputs changed.

        Called after a config reload. Preserves warm in-memory context for every
        chat the change didn't touch, instead of clearing the whole cache.
        Returns the number of agents dropped.
        """
        dropped = 0
        for chat_id in list(self._chat_agents.keys()):
            current = self._chat_config_fingerprint(chat_id)
            if self._chat_agent_fingerprints.get(chat_id) != current:
                self._chat_agents.pop(chat_id, None)
                self._chat_agent_fingerprints.pop(chat_id, None)
                dropped += 1
        return dropped

    def _track_payloads(self, chat_id: "int | None", payloads: "list[str]") -> None:
        """Record tool/fetch payloads for this chat so _on_agent_result can strip echoes."""
        if chat_id is None or not payloads:
            return
        bucket = self._recent_tool_payloads.setdefault(chat_id, [])
        bucket.extend(payloads)
        if len(bucket) > _MAX_TRACKED_PAYLOADS:
            del bucket[:-_MAX_TRACKED_PAYLOADS]

    async def _exec_tool_tags(self, result: str, user_id: int | None = None, chat_id: int | None = None) -> "tuple[str, str | None]":
        """Delegate to tasks.executor.exec_tool_tags — thin daemon wrapper."""
        return await _exec_tool_tags_fn(
            result,
            user_id=user_id,
            chat_id=chat_id,
            claude_guard_action=self._claude_guard_action,
            track_payloads=self._track_payloads,
        )


    async def _execute_output_tags(
        self, task: KanbanTask, tc: "_TagCollection", sent_msg_id: int
    ) -> None:
        from .tasks.output_handler import execute_output_tags
        await execute_output_tags(self, task, tc, sent_msg_id)

    async def _claude_guard_action(self, action: str) -> str:
        """Call claude-guard for status/trigger. Returns result string."""
        dg = config.section("system").get("claude_guard", {})
        url = dg.get("url", "").rstrip("/")
        token = dg.get("token", "")
        if not url or not token:
            return "claude_guard not configured (system.claude_guard.url and .token required)"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                if action == "status":
                    resp = await client.get(f"{url}/api/deploy/status?token={token}")
                else:
                    resp = await client.post(f"{url}/deploy?token={token}")
                if resp.status_code == 200:
                    try:
                        return str(resp.json())
                    except Exception:
                        return resp.text[:200]
                return f"HTTP {resp.status_code}: {resp.text[:100]}"
        except Exception as e:
            return f"claude_guard error: {e}"

    async def _do_git_clone(self, chat_id: int, repo_url: str, target: str) -> None:
        try:
            os.makedirs(_PLUGINS_DIR, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *_build_git_clone_cmd(repo_url, target),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
            if proc.returncode == 0:
                clone_msg = f"✓ Cloned to `{target}`"
            else:
                err = stderr.decode(errors="replace")[:300]
                clone_msg = f"Git clone failed:\n`{err}`"
            try:
                await self._api.send_message(chat_id, _md_to_telegram_html(clone_msg), parse_mode="HTML")
            except Exception:
                await self._api.send_message(chat_id, clone_msg)
        except asyncio.TimeoutError:
            await self._api.send_message(chat_id, "Git clone timed out (120s).")
        except Exception as e:
            logger.warning("Git clone error for %s: %s", repo_url, e)
            await self._api.send_message(chat_id, _user_error("Repository konnte nicht geklont werden", e))

    async def _notify_admin_new_user(self, telegram_id: int, name: str | None) -> None:
        cfg = config.section("users")
        tg_cfg = config.section("telegram")
        admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))
        for admin_id in admin_ids:
            await self._api.send_message(
                admin_id,
                f"New user requesting access: {name or 'unknown'} (ID: {telegram_id})\n/auth {telegram_id}",
            )

    async def _notify_admins_kb_quarantine(self, entry_id: int, title: str, chat_id: int, trust: int) -> None:
        """Admin über quarantänierten KB-Eintrag informieren (Freigabe/Löschung per Button)."""
        cfg = config.section("users")
        tg_cfg = config.section("telegram")
        admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))
        label = trust_mod.TRUST_LABELS.get(trust, str(trust))
        msg = (
            f"🧪 KB-Quarantäne: Eintrag '{title}' aus Chat {chat_id} "
            f"(Trust {trust} – {label}). Freigeben oder löschen?"
        )
        keyboard = {"inline_keyboard": [[
            {"text": "✅ Freigeben", "callback_data": f"kb_approve:{entry_id}"},
            {"text": "🗑 Löschen", "callback_data": f"kb_reject:{entry_id}"},
        ]]}
        for admin_id in admin_ids:
            try:
                await self._api.send_message(admin_id, msg, reply_markup=keyboard)
            except Exception as e:
                logger.warning("KB quarantine notification to admin %d failed: %s", admin_id, e)

    async def _log_approval(self, *, action_types, content_preview, task_id, chat_id, decision, decided_by, requested_at, decided_at) -> None:
        try:
            await self._conn.execute(
                """INSERT INTO approval_log
                   (action_types, content_preview, task_id, chat_id, decision, decided_by, requested_at, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (json.dumps(action_types), content_preview, task_id, chat_id, decision, decided_by, requested_at, decided_at),
            )
            await self._conn.commit()
        except Exception as e:
            logger.warning("approval_log write failed: %s", e)

    def _flush_chat_queue(self, chat_id: int) -> None:
        """Spawn next queued chat message, if any."""
        self._chat_task_start_times.pop(chat_id, None)
        queue = self._pending_chat_queue.get(chat_id, [])
        if not queue:
            self._pending_chat_queue.pop(chat_id, None)
            self._active_chat_content.pop(chat_id, None)
            return
        user_id, content, msg_id = queue.pop(0)
        if not queue:
            self._pending_chat_queue.pop(chat_id, None)
        self._active_chat_content[chat_id] = content
        self._chat_task_start_times[chat_id] = time.time()
        asyncio.create_task(
            self._handle_chat(chat_id, user_id, content, msg_id),
            name=f"chat-{chat_id}",
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
        from .background import typing_loop
        await typing_loop(self, chat_id)

    async def _config_watcher_loop(self) -> None:
        from .background import config_watcher_loop
        await config_watcher_loop(self)

    async def _usage_poll_loop(self) -> None:
        from .background import usage_poll_loop
        await usage_poll_loop(self)

    _TOR_CHECK_INTERVAL = 60
    _TOR_TASK_COOLDOWN = 300

    async def _stuck_chat_watchdog(self) -> None:
        from .background import stuck_chat_watchdog
        await stuck_chat_watchdog(self)

    async def _reminder_watcher(self) -> None:
        from .background import reminder_watcher
        await reminder_watcher(self)

    async def _network_health_loop(self, admin_ids: list) -> None:
        from .background import network_health_loop
        await network_health_loop(self, admin_ids)

    async def _notify_admins_usage(self, stats) -> None:
        from .background import notify_admins_usage
        await notify_admins_usage(self, stats)


async def run() -> None:
    _setup_logging()
    daemon = Daemon()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.stop()))

    await daemon.run_forever()


if __name__ == "__main__":
    asyncio.run(run())
