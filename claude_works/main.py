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

from . import config, db
from .mode import DaemonMode, ModeManager, detect_startup_mode
from .telegram.api import TelegramAPI
from .telegram.poller import TelegramPoller
from .telegram.reactions import resolve_action, extract_reaction_emoji
from .tasks.models import IncomingMessage
from .tasks.bundler import should_bundle, merge_content
from .kanban.board import KanbanBoard
from .kanban.models import AgentClass, KanbanTask
from .telemetry.tokens import TokenTracker
from .knowledge import store as knowledge_store
from .agents.coordinator import AgentCoordinator
from .agents.mechanic import MechanicAgent, MechanicContext
from .agents.specialist.generalist import GeneralistAgent
from .auth.users import upsert_user, is_allowed, is_admin, set_role
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


_ECHOED_TOOL_RE = re.compile(
    r"GitHub\s+(?:GET|POST|PUT|PATCH|DELETE)\s+[^\n]+:\n\s*[\[\{][\s\S]*?(?=\n[^\s\[\{]|\Z)",
    re.MULTILINE,
)

def _strip_echoed_tool_results(text: str) -> str:
    """Remove raw tool-result blocks the agent may have echoed into its reply."""
    return _ECHOED_TOOL_RE.sub("[tool output stripped]", text).strip()


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


def _extract_send_email_tag(text: str) -> "tuple[str, tuple[str, str, str] | None]":
    """Extract [SEND_EMAIL: to | subject | body]. Returns (clean_text, (to, subject, body) or None)."""
    m = re.search(r'\[SEND_EMAIL:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    parts = [p.strip() for p in m.group(1).split('|', 2)]
    if len(parts) < 2:
        return text, None
    to = parts[0]
    subject = parts[1]
    body = parts[2] if len(parts) > 2 else ""
    return clean, (to, subject, body)


def _extract_read_email_tag(text: str) -> "tuple[str, tuple[str, int] | None]":
    """Extract [READ_EMAIL: folder | count]. Returns (clean_text, (folder, count) or None)."""
    m = re.search(r'\[READ_EMAIL:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    parts = [p.strip() for p in m.group(1).split('|', 1)]
    folder = parts[0] or "INBOX"
    try:
        count = int(parts[1]) if len(parts) > 1 else 5
    except ValueError:
        count = 5
    return clean, (folder, min(count, 20))


def _extract_github_api_tag(text: str) -> "tuple[str, tuple[str, str, str] | None]":
    """Extract [GITHUB_API: METHOD | /endpoint | body]. Returns (clean_text, (method, endpoint, body) or None)."""
    m = re.search(r'\[GITHUB_API:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    parts = [p.strip() for p in m.group(1).split('|', 2)]
    if len(parts) < 2:
        return text, None
    method = parts[0].upper()
    endpoint = parts[1]
    body = parts[2] if len(parts) > 2 else ""
    return clean, (method, endpoint, body)


def _extract_git_clone_tag(text: str) -> "tuple[str, tuple[str, str] | None]":
    """Extract [GIT_CLONE: repo_url | plugin_name]. Returns (clean_text, (url, name) or None)."""
    m = re.search(r'\[GIT_CLONE:\s*([^\]|]+?)\s*\|\s*([^\]]+?)\s*\]', text)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    return clean, (m.group(1).strip(), m.group(2).strip())


def _extract_kb_search_tag(text: str) -> "tuple[str, str | None]":
    """Extract [KB_SEARCH: query]. Returns (clean_text, query or None)."""
    m = re.search(r'\[KB_SEARCH:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    return clean, m.group(1).strip()


def _extract_kb_save_tag(text: str) -> "tuple[str, tuple[str, str, list, str] | None]":
    """Extract [KB_SAVE: title | type | tags | content]. Returns (clean_text, (title, type, tags, content) or None)."""
    m = re.search(r'\[KB_SAVE:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    parts = [p.strip() for p in m.group(1).split('|', 3)]
    if not parts[0]:
        return text, None
    title = parts[0]
    entry_type = parts[1] if len(parts) > 1 and parts[1] else "note"
    raw_tags = parts[2] if len(parts) > 2 else ""
    content = parts[3] if len(parts) > 3 else ""
    tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
    return clean, (title, entry_type, tags, content)


def _extract_kb_update_tag(text: str) -> "tuple[str, tuple | None]":
    """Extract [KB_UPDATE: id | title | type | tags | content]. Returns (clean_text, (id, title, type, tags, content) or None)."""
    m = re.search(r'\[KB_UPDATE:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    parts = [p.strip() for p in m.group(1).split('|', 4)]
    try:
        entry_id = int(parts[0])
    except (ValueError, IndexError):
        return text, None
    title = parts[1] if len(parts) > 1 and parts[1] else None
    entry_type = parts[2] if len(parts) > 2 and parts[2] else None
    raw_tags = parts[3] if len(parts) > 3 else None
    tags = [t.strip() for t in raw_tags.split(',') if t.strip()] if raw_tags else None
    content = parts[4] if len(parts) > 4 and parts[4] else None
    return clean, (entry_id, title, entry_type, tags, content)


_CONFIG_UPDATE_BLOCKED = {"telegram.token", "web.auth_token", "llm.api_key"}


def _extract_config_update_tag(text: str) -> "tuple[str, tuple[str, str] | None]":
    """Extract [CONFIG_UPDATE: dotted.path | value_json]. Returns (clean_text, (path, value_json) or None)."""
    m = re.search(r'\[CONFIG_UPDATE:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    parts = [p.strip() for p in m.group(1).split('|', 1)]
    if len(parts) < 2:
        return text, None
    return clean, (parts[0], parts[1])


def _extract_plugin_config_get_tag(text: str) -> "tuple[str, str | None]":
    """Extract [PLUGIN_CONFIG_GET: plugin_name]. Returns (clean_text, plugin_name or None)."""
    m = re.search(r'\[PLUGIN_CONFIG_GET:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    return clean, m.group(1).strip()


def _extract_plugin_config_set_tag(text: str) -> "tuple[str, tuple[str, dict] | None]":
    """Extract [PLUGIN_CONFIG_SET: plugin_name | {json}]. Returns (clean_text, (name, dict) or None)."""
    m = re.search(r'\[PLUGIN_CONFIG_SET:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()
    parts = [p.strip() for p in m.group(1).split('|', 1)]
    if len(parts) < 2:
        return text, None
    plugin_name = parts[0]
    try:
        import json as _json
        cfg_dict = _json.loads(parts[1])
        if not isinstance(cfg_dict, dict):
            return text, None
    except Exception:
        return text, None
    return clean, (plugin_name, cfg_dict)


_PLUGINS_DIR = os.environ.get("PLUGINS_DIR", "/data/plugins")
_URL_RE = re.compile(r'https?://[^\s<>"\']+')
_MAX_FETCH_URLS = 3
_MAX_FETCH_CHARS = 4000
_TOR_SOCKS_DEFAULT = "socks5://127.0.0.1:9050"


def _build_git_clone_cmd(repo_url: str, target: str) -> list[str]:
    """Build git clone command, optionally routed through Tor if tor_socks_proxy is configured."""
    tor_proxy = config.section("security").get("tor_socks_proxy", "")
    if tor_proxy:
        # socks5h:// resolves DNS through proxy, not locally
        git_proxy = tor_proxy.replace("socks5://", "socks5h://")
        return ["git", "-c", f"http.proxy={git_proxy}", "clone", "--depth=1", repo_url, target]
    return ["git", "clone", "--depth=1", repo_url, target]

_TOR_RESTART_RE = re.compile(r'\[TOR_RESTART\]', re.IGNORECASE)


def _extract_tor_restart_tag(text: str) -> "tuple[str, bool]":
    """Remove [TOR_RESTART] from text. Returns (clean_text, found)."""
    clean, n = _TOR_RESTART_RE.subn("", text)
    return clean.strip(), n > 0


async def _restart_tor() -> str:
    """Start or restart Tor daemon inside container. Returns status string."""
    try:
        import os as _os
        _os.makedirs("/var/lib/tor", exist_ok=True)
        _os.makedirs("/run/tor", exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "tor", "--RunAsDaemon", "1",
            "--DataDirectory", "/var/lib/tor",
            "--PidFile", "/run/tor/tor.pid",
            "--Log", "warn stderr",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode != 0:
            return f"tor start failed (exit {proc.returncode}): {stderr.decode(errors='replace')[:200]}"
        # Wait for SOCKS port to open (max 60s)
        for _ in range(60):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", 9050), timeout=1.0
                )
                writer.close()
                await writer.wait_closed()
                return "Tor started successfully — SOCKS5 proxy ready on 127.0.0.1:9050"
            except Exception:
                await asyncio.sleep(1.0)
        return "tor process started but port 9050 not ready after 60s — Tor may still be bootstrapping"
    except asyncio.TimeoutError:
        return "tor start timed out (10s)"
    except Exception as e:
        return f"tor restart error: {e}"


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


async def _fetch_url_content(url: str, proxy: str | None = None) -> str | None:
    try:
        client_kwargs: dict = {"timeout": 15.0, "follow_redirects": True}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; claude-works/1.0)"})
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "text" not in ct and "json" not in ct:
                return None
            text = resp.text
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'&(?:[a-z]+|#\d+);', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:_MAX_FETCH_CHARS] if text else None
    except Exception as e:
        logger.debug("URL fetch failed proxy=%s (%s): %s", proxy, url, e)
        return None


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
        self._pending_direct_fetches: dict[str, dict] = {}
        self._pending_reauth: dict[int, asyncio.subprocess.Process] = {}  # chat_id → proc
        self._chat_agents: dict[int, GeneralistAgent] = {}  # chat_id → persistent chat agent
        self._bot_username: str = ""  # loaded at startup via getMe
        self._mention_only_chats: set[int] = set()  # chat_ids where bot only responds to @mention
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
        """Initialize all runtime components. Called in RUN mode."""
        self._mode_mgr.transition(DaemonMode.RUN)

        from .prompts import export_defaults as _export_prompts
        _export_prompts()

        self._conn = await db.init()

        from .knowledge.store import import_from_directory as _kb_import
        _imported = await _kb_import(self._conn)
        if _imported:
            logger.info("Imported %d knowledge file(s) from /data/knowledge/", _imported)
        tg_cfg = config.section("telegram")
        self._api = TelegramAPI(tg_cfg["token"])

        try:
            me = await self._api.get_me()
            self._bot_username = me.get("result", {}).get("username", "") or me.get("username", "")
            logger.info("Bot username: @%s", self._bot_username)
        except Exception as e:
            logger.warning("getMe failed: %s", e)

        await self._load_mention_only_chats()

        self._board = KanbanBoard(self._conn)
        self._token_tracker = TokenTracker(self._conn)
        await self._reset_stale_tasks()

        self._coordinator = AgentCoordinator(
            board=self._board,
            token_tracker=self._token_tracker,
            on_result=self._on_agent_result,
            on_requeue=self._on_task_requeued,
            user_backgrounds=self._user_backgrounds,
            exec_tools=self._exec_tool_tags,
        )
        self._coordinator.start()

        startup_ts = int(time.time())
        self._poller = TelegramPoller(self._api, self._on_update, skip_before_ts=startup_ts)
        self._poller.start()

        cfg = config.section("users")
        tg_cfg = config.section("telegram")
        admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))

        # Post-import KB classification check
        if admin_ids:
            async with self._conn.execute(
                "SELECT COUNT(*) FROM knowledge WHERE source LIKE 'file::%' AND (tags IS NULL OR tags = '[]')"
            ) as cur:
                row = await cur.fetchone()
            unclassified = row[0] if row else 0
            if unclassified > 0:
                migration_task = KanbanTask(
                    id=None,
                    chat_id=admin_ids[0],
                    user_id=admin_ids[0],
                    content=(
                        f"## Knowledge Base: Classify {unclassified} Imported Entries\n\n"
                        f"There are {unclassified} knowledge base entries imported from files "
                        f"(source=file::*, no tags) that need proper classification.\n\n"
                        f"For each entry:\n"
                        f"1. Use [KB_SEARCH: document] to find untagged entries\n"
                        f"2. Read content and determine:\n"
                        f"   - Best type: note / fact / procedure / context / document\n"
                        f"   - Relevant tags (comma-separated, descriptive)\n"
                        f"3. Update: [KB_UPDATE: <id> | <title> | <type> | <tags> | ]\n"
                        f"   (leave content field empty to keep original)\n\n"
                        f"Goal: make entries discoverable via FTS search. "
                        f"Prefer specific types over 'document'. Add topic tags."
                    ),
                    priority=0,
                )
                await self._board.push(migration_task)
                logger.info("Pushed KB classification task for %d untagged file-imported entries", unclassified)

        # Startup CLI auth check
        llm_cfg = config.section("llm")
        if llm_cfg.get("provider") == "cli":
            asyncio.create_task(self._check_cli_auth_on_startup(admin_ids), name="startup-auth-check")
        self._security.configure(self._api.send_message, admin_ids)

        asyncio.create_task(self._config_watcher_loop(), name="config-watcher")
        asyncio.create_task(self._usage_poll_loop(), name="usage-poller")
        asyncio.create_task(self._network_health_loop(admin_ids), name="network-health")

        self._running = True
        logger.info("claude-works daemon started in RUN mode")

        for admin_id in admin_ids:
            try:
                await self._api.send_message(admin_id, "✓ claude-works started and ready.")
            except Exception as e:
                logger.warning("Startup notification to admin %d failed: %s", admin_id, e)

    async def _reset_stale_tasks(self) -> None:
        """Reset tasks interrupted by previous crash/restart so they're retried cleanly."""
        from .kanban.models import Lane
        # Clear stale hourglass reactions from previous run
        async with self._conn.execute("SELECT task_id, chat_id, tg_msg_id FROM pending_reactions") as cur:
            stale_reactions = await cur.fetchall()
        for _, chat_id, tg_msg_id in stale_reactions:
            try:
                await self._api.set_message_reaction(chat_id, tg_msg_id, None)
            except Exception:
                pass
        if stale_reactions:
            await self._conn.execute("DELETE FROM pending_reactions")
            await self._conn.commit()
            logger.info("Startup cleanup: cleared %d stale hourglass reactions", len(stale_reactions))

        # Update stale initial messages to show restart notice
        async with self._conn.execute("SELECT task_id, chat_id, tg_msg_id FROM pending_initial_msgs") as cur:
            stale_initials = await cur.fetchall()
        for _, chat_id, tg_msg_id in stale_initials:
            try:
                await self._api.edit_message(chat_id, tg_msg_id, "↩ Restarted — task re-queued, working on it...")
            except Exception:
                pass
        if stale_initials:
            await self._conn.execute("DELETE FROM pending_initial_msgs")
            await self._conn.commit()
            logger.info("Startup cleanup: updated %d stale initial messages", len(stale_initials))

        stale_lanes = (Lane.ASSIGNED.value, Lane.IN_PROGRESS.value, Lane.REVIEW.value)
        placeholders = ",".join("?" * len(stale_lanes))
        # Reset root tasks to BACKLOG
        async with self._conn.execute(
            f"""UPDATE kanban_tasks SET lane = ?, agent_class = NULL, agent_id = NULL,
                started_at = NULL, assigned_at = NULL
                WHERE lane IN ({placeholders}) AND parent_id IS NULL""",
            (Lane.BACKLOG.value, *stale_lanes),
        ) as cur:
            root_reset = cur.rowcount
        await self._conn.commit()
        # Remove orphaned child tasks — their parent will re-decompose
        async with self._conn.execute(
            f"DELETE FROM kanban_tasks WHERE lane IN ({placeholders}) AND parent_id IS NOT NULL",
            stale_lanes,
        ) as cur:
            children_removed = cur.rowcount
        await self._conn.commit()
        if root_reset or children_removed:
            logger.info(
                "Startup cleanup: %d root tasks → BACKLOG, %d orphaned children removed",
                root_reset, children_removed,
            )

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

    async def _build_status_snapshot(self) -> str:
        """Build a concise live system status block to inject into admin chat context."""
        lines = []
        # Mode
        sys_mode = config.get().get("system", {}).get("mode", "run").upper()
        lines.append(f"Mode: {'▶ RUN' if sys_mode == 'RUN' else '⚠ ' + sys_mode}")
        # Active agents
        active = self._coordinator.active_count if self._coordinator else 0
        lines.append(f"Agents: {active} active")
        # Kanban queue stats
        try:
            async with self._conn.execute(
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
        # Tor status
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 9050), timeout=2.0
            )
            writer.close()
            lines.append("Tor: ✅ up")
        except Exception:
            lines.append("Tor: ❌ port 9050 unreachable")
        # Rate limit
        if self._coordinator and self._coordinator.is_rate_limited:
            lines.append("LLM: ⏳ rate limited")
        else:
            llm_provider = config.get().get("llm", {}).get("provider", "?")
            usage_pct = ""
            if self._usage_state and self._usage_state.usage_pct is not None:
                usage_pct = f" ({int(self._usage_state.usage_pct * 100)}% limit used)"
            lines.append(f"LLM: ✅ {llm_provider}{usage_pct}")
        ts = time.strftime("%H:%M:%S", time.localtime())
        return f"[SYSTEM SNAPSHOT {ts}]\n" + "\n".join(lines)

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

    async def web_admin_chat(self, message: str) -> str:
        """Process admin message from web UI, return reply. Maintains multi-turn context."""
        if self._web_admin_agent is None:
            from .prompts import load as _load_prompt
            uplink_persona = self._UPLINK_PERSONA_PREFIX + _load_prompt("generalist")
            self._web_admin_agent = GeneralistAgent(
                task_id=0,
                user_context={"user_id": -1, "chat_id": -1, "caveman_mode": False},
                agent_class="chief",
                persona=uplink_persona,
            )
        now = int(time.time())
        await self._conn.execute(
            "INSERT INTO admin_chat_messages (role, content, sent_at) VALUES (?, ?, ?)",
            ("user", message, now),
        )
        await self._conn.commit()
        snapshot = await self._build_status_snapshot()
        enriched = f"{snapshot}\n\n---\n\n{message}"
        reply = await self._web_admin_agent.run(enriched)
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

        # Check if user is completing CLI re-auth
        if chat_id in self._pending_reauth and text and not text.startswith("/"):
            proc = self._pending_reauth.pop(chat_id)
            if proc.returncode is not None:
                await self._api.send_message(chat_id, "Auth session expired. Run /reauth again.")
                return
            try:
                proc.stdin.write((text.strip() + "\n").encode())
                await proc.stdin.drain()
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
                if proc.returncode == 0:
                    await self._api.send_message(chat_id, "✓ Claude CLI authenticated.")
                else:
                    out = stdout.decode(errors="replace") if stdout else ""
                    await self._api.send_message(chat_id, f"Auth failed: {out[:200]}")
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                await self._api.send_message(chat_id, "Auth confirmation timed out. Try /reauth again.")
            return

        # In REPAIR/MIGRATE mode, route messages to Mechanic if admin
        if self._mode_mgr.mode in (DaemonMode.REPAIR, DaemonMode.MIGRATE):
            if await is_admin(self._conn, telegram_id) and self._mechanic and text:
                reply = await self._mechanic.followup(text)
                clean_reply, keyboard = _parse_buttons(reply)
                reply_markup = {"inline_keyboard": keyboard} if keyboard else None
                await self._api.send_message(chat_id, clean_reply[:4096], reply_markup=reply_markup)
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

        cursor = await self._conn.execute(
            """INSERT OR IGNORE INTO messages (telegram_message_id, chat_id, from_user_id, text, voice_file_id, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (incoming.telegram_message_id, incoming.chat_id, incoming.from_user_id,
             incoming.text, incoming.voice_file_id, incoming.timestamp),
        )
        await self._conn.commit()
        if cursor.rowcount == 0:
            # Duplicate telegram_message_id — already processed (e.g. edited_message re-delivery)
            logger.debug("Duplicate message_id=%d — skipping", incoming.telegram_message_id)
            return

        # Mention-only mode: log message to DB (done above) but skip response unless @mentioned
        if chat_id in self._mention_only_chats:
            is_reply_to_bot = bool(msg.get("reply_to_message", {}).get("from", {}).get("is_bot"))
            is_mentioned = bool(
                self._bot_username and text and f"@{self._bot_username}" in text
            )
            if not is_mentioned and not is_reply_to_bot:
                logger.debug("Mention-only: silently logged msg in chat=%d", chat_id)
                return
            # Strip @mention from text so agent doesn't see it as part of the request
            if self._bot_username and text:
                text = text.replace(f"@{self._bot_username}", "").strip()

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
                api_key = config.section("tts").get("elevenlabs_api_key", "")
                transcript = await _transcribe_audio(api_key, audio_bytes)
                if transcript:
                    content = transcript + ("\n" + content if content else "")
                else:
                    content = "[Voice message — transcription unavailable]" + ("\n" + content if content else "")
            except Exception as e:
                logger.warning("Voice download/transcription error: %s", e)
                content = "[Voice message — transcription failed]" + ("\n" + content if content else "")

        urls = _URL_RE.findall(content)
        if urls:
            tor_proxy = config.section("security").get("tor_socks_proxy", "") or None
            fetched_sections: list[str] = []
            urls_blocked: list[str] = []
            for url in urls[:_MAX_FETCH_URLS]:
                page_text = await _fetch_url_content(url, proxy=tor_proxy)
                if page_text is not None:
                    fetched_sections.append(f"[Content of {url}]\n{page_text}")
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
                self._pending_direct_fetches[fetch_hash] = {
                    "chat_id": chat_id,
                    "user_id": telegram_id,
                    "content": content,
                    "urls": urls_blocked,
                    "expires_at": time.time() + 300,
                }
                domains = ", ".join(urllib.parse.urlparse(u).netloc for u in urls_blocked)
                await self._api.send_message(
                    chat_id,
                    f"🔒 Tor access failed: <code>{domains}</code>\nAllow direct access?",
                    parse_mode="HTML",
                    reply_markup={"inline_keyboard": [[
                        {"text": "✅ Yes", "callback_data": f"direct:{fetch_hash}"},
                        {"text": "❌ Skip", "callback_data": f"deny:{fetch_hash}"},
                    ]]}
                )
                return

        if _is_task(content):
            task_content = content
            recent = await self._load_chat_history(chat_id, limit=10)
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
            task_id = await self._board.push(task)
            try:
                await self._api.set_message_reaction(chat_id, incoming.telegram_message_id, "⏳")
                self._pending_reactions[task_id] = (chat_id, incoming.telegram_message_id)
                await self._conn.execute(
                    "INSERT OR REPLACE INTO pending_reactions (task_id, chat_id, tg_msg_id) VALUES (?, ?, ?)",
                    (task_id, chat_id, incoming.telegram_message_id),
                )
                await self._conn.commit()
            except Exception:
                pass
            # Send initial "in progress" message so something visible appears immediately
            try:
                preview = content[:120] + ("…" if len(content) > 120 else "")
                init_sent = await self._api.send_message(
                    chat_id, f"✎ Working on: {preview}"
                )
                init_msg_id = init_sent["message_id"]
                self._pending_initial_msgs[task_id] = init_msg_id
                await self._conn.execute(
                    "INSERT OR REPLACE INTO pending_initial_msgs (task_id, chat_id, tg_msg_id) VALUES (?, ?, ?)",
                    (task_id, chat_id, init_msg_id),
                )
                await self._conn.commit()
            except Exception:
                pass
        else:
            if chat_id in self._typing_tasks:
                self._pending_chat_queue.setdefault(chat_id, []).append(
                    (telegram_id, content, incoming.telegram_message_id)
                )
                current = self._active_chat_content.get(chat_id, "")
                snippet = (current[:60] + "…") if len(current) > 60 else current
                snippet = snippet.replace("\n", " ")
                await self._api.send_message(
                    chat_id,
                    f"⏳ Noch beschäftigt mit: „{snippet}“\nDeine Nachricht kommt danach dran.",
                )
            else:
                self._active_chat_content[chat_id] = content
                asyncio.create_task(
                    self._handle_chat(chat_id, telegram_id, content, incoming.telegram_message_id),
                    name=f"chat-{chat_id}",
                )

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
                if sec_orig_id and sec_orig_text:
                    try:
                        await self._api.edit_message(
                            chat_id, sec_orig_id,
                            f"{sec_orig_text}\n\n→ {reply}",
                            remove_keyboard=True,
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
        parts = text.strip().split()
        # Strip @botname suffix from commands sent in group chats (e.g. /mention@botname)
        raw_cmd = parts[0].lower()
        if "@" in raw_cmd:
            raw_cmd = raw_cmd.split("@")[0]
        cmd = raw_cmd

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

        elif cmd == "/mention":
            if not await is_allowed(self._conn, from_id):
                return
            arg = parts[1].lower() if len(parts) >= 2 else ""
            if arg == "on":
                self._mention_only_chats.add(chat_id)
                await self._save_mention_only_chats()
                await self._api.send_message(chat_id, "👂 Mention-only mode active — responding only when @mentioned.")
            elif arg == "off":
                self._mention_only_chats.discard(chat_id)
                await self._save_mention_only_chats()
                await self._api.send_message(chat_id, "💬 Now responding to all messages.")
            else:
                state = "on" if chat_id in self._mention_only_chats else "off"
                await self._api.send_message(chat_id, f"Mention-only mode: {state}\nUsage: /mention on|off")

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

        elif cmd == "/reauth":
            if not await is_admin(self._conn, from_id):
                await self._api.send_message(chat_id, "Nur für Admins.")
                return
            await self._start_telegram_reauth(chat_id)
            return

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
            await self._api.send_message(chat_id, f"Auth start failed: {exc}")
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
        if task.id in self._chat_task_ids:
            self._chat_task_ids.discard(task.id)
            self._stop_typing(task.chat_id)
            self._flush_chat_queue(task.chat_id)
        reaction_info = self._pending_reactions.pop(task.id, None) if task.id else None
        if reaction_info:
            try:
                await self._api.set_message_reaction(reaction_info[0], reaction_info[1], None)
            except Exception:
                pass
            try:
                await self._conn.execute("DELETE FROM pending_reactions WHERE task_id = ?", (task.id,))
                await self._conn.commit()
            except Exception:
                pass
        # Resolve reply-to: board tasks use original message from pending_reactions;
        # chat tasks use the stored reply_to tracked via _chat_reply_to.
        reply_to_id: int | None = (
            reaction_info[1] if reaction_info else self._chat_reply_to.pop(task.id, None)
        )

        if task.parent_id is not None:
            return
        if result:
            allowed = await self._security.check(
                result, task_id=task.id, chat_id=task.chat_id, user_id=task.user_id
            )
            if not allowed:
                await self._api.send_message(task.chat_id, "Response blocked by security policy.")
                return
            # Strip all tags from clean_result (collect lists), then send clean text
            clean_result, keyboard = _parse_buttons(result)

            all_tts: list[str] = []
            while True:
                clean_result, v = _extract_voice_tag(clean_result)
                if not v: break
                all_tts.append(v)

            all_maps: list[str] = []
            while True:
                clean_result, v = _extract_map_tag(clean_result)
                if not v: break
                all_maps.append(v)

            all_send_emails: list[tuple] = []
            while True:
                clean_result, v = _extract_send_email_tag(clean_result)
                if not v: break
                all_send_emails.append(v)

            # READ_EMAIL is a read-only tool — handled in _exec_tool_tags; strip if it somehow survived
            while True:
                clean_result, v = _extract_read_email_tag(clean_result)
                if not v: break

            all_github: list[tuple] = []
            while True:
                clean_result, v = _extract_github_api_tag(clean_result)
                if not v: break
                all_github.append(v)

            # GIT_CLONE is a read tool — handled in _exec_tool_tags; strip if it somehow survived
            while True:
                clean_result, v = _extract_git_clone_tag(clean_result)
                if not v: break

            all_kb_saves: list[tuple] = []
            while True:
                clean_result, v = _extract_kb_save_tag(clean_result)
                if not v: break
                all_kb_saves.append(v)

            all_kb_updates: list[tuple] = []
            while True:
                clean_result, v = _extract_kb_update_tag(clean_result)
                if not v: break
                all_kb_updates.append(v)

            all_plugin_config_sets: list[tuple] = []
            while True:
                clean_result, v = _extract_plugin_config_set_tag(clean_result)
                if not v: break
                all_plugin_config_sets.append(v)

            all_config_updates: list[tuple] = []
            while True:
                clean_result, v = _extract_config_update_tag(clean_result)
                if not v: break
                all_config_updates.append(v)

            reply_markup = {"inline_keyboard": keyboard} if keyboard is not None else None
            initial_msg_id = self._pending_initial_msgs.pop(task.id, None) if task.id else None
            if initial_msg_id and task.id:
                try:
                    await self._conn.execute("DELETE FROM pending_initial_msgs WHERE task_id = ?", (task.id,))
                    await self._conn.commit()
                except Exception:
                    pass

            # Strip raw tool-result blocks the agent may have echoed back
            clean_result = _strip_echoed_tool_results(clean_result)

            if clean_result.strip():
                html_result = _md_to_telegram_html(clean_result)
                if initial_msg_id:
                    try:
                        await self._api.edit_message(task.chat_id, initial_msg_id, html_result, parse_mode="HTML", reply_markup=reply_markup)
                        sent = {"message_id": initial_msg_id}
                    except Exception:
                        try:
                            sent = await self._api.send_message(task.chat_id, html_result, parse_mode="HTML", reply_markup=reply_markup, reply_to_message_id=reply_to_id)
                        except Exception:
                            sent = await self._api.send_message(task.chat_id, clean_result, reply_markup=reply_markup, reply_to_message_id=reply_to_id)
                else:
                    try:
                        sent = await self._api.send_message(task.chat_id, html_result, parse_mode="HTML", reply_markup=reply_markup, reply_to_message_id=reply_to_id)
                    except Exception:
                        logger.warning("HTML send failed for task=%d, retrying plain", task.id)
                        sent = await self._api.send_message(task.chat_id, clean_result, reply_markup=reply_markup, reply_to_message_id=reply_to_id)
            else:
                if initial_msg_id:
                    try:
                        await self._api.edit_message(task.chat_id, initial_msg_id, "✓")
                    except Exception:
                        pass
                sent = {"message_id": initial_msg_id or 0}

            for tts_text in all_tts:
                tts_allowed = await self._security.check_action(
                    "tts_send", tts_text, task_id=task.id, chat_id=task.chat_id, user_id=task.user_id
                )
                if tts_allowed:
                    try:
                        tts_cfg = config.section("tts")
                        audio = await _synthesize_tts(tts_text, tts_cfg)
                        if audio:
                            await self._api.send_voice(task.chat_id, audio)
                    except Exception as e:
                        logger.warning("TTS failed for task=%d: %s", task.id, e)
                else:
                    logger.info("TTS blocked by security for task=%d", task.id)

            for map_query in all_maps:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as hc:
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
                        await self._api.send_message(task.chat_id, f"📍 {map_query} — not found.")
                except Exception as e:
                    logger.warning("Map geocoding failed for task=%d: %s", task.id, e)

            for to, subject, body in all_send_emails:
                email_content = f"To: {to}\nSubject: {subject}\n\n{body}"
                email_allowed = await self._security.check_action(
                    "email_send", email_content, task_id=task.id, chat_id=task.chat_id, user_id=task.user_id
                )
                if not email_allowed:
                    logger.info("Email send blocked by security officer for task=%d", task.id)
                    await self._api.send_message(task.chat_id, "Email blocked by security officer — possible data leak detected.")
                else:
                    try:
                        email_cfg = config.section("email")
                        await _send_email(to, subject, body, email_cfg)
                        await self._api.send_message(task.chat_id, f"✉️ Email sent to {to}.")
                    except KeyError:
                        logger.error("Email config missing — set email.smtp_host/user/password in settings.json")
                        await self._api.send_message(task.chat_id, "Email not sent: email configuration missing.")
                    except Exception as e:
                        logger.warning("Email send failed for task=%d: %s", task.id, e)
                        await self._api.send_message(task.chat_id, f"Email send failed: {e}")

            for method, endpoint, body in all_github:
                is_write = method in ("POST", "PUT", "PATCH", "DELETE")
                do_exec = True
                if is_write:
                    gh_content = f"{method} {endpoint}\n\n{body or ''}"
                    gh_allowed = await self._security.check_action(
                        "github_write", gh_content, task_id=task.id, chat_id=task.chat_id, user_id=task.user_id
                    )
                    if not gh_allowed:
                        logger.info("GitHub write blocked by security officer for task=%d", task.id)
                        await self._api.send_message(task.chat_id, "GitHub write blocked by security officer — possible data leak detected.")
                        do_exec = False
                if do_exec:
                    try:
                        github_cfg = config.section("github")
                        result_data = await _github_api(method, endpoint, body or None, github_cfg)
                        import json as _json
                        result_preview = _json.dumps(result_data, ensure_ascii=False, indent=2)[:1200]
                        await self._api.send_message(task.chat_id, f"GitHub `{method} {endpoint}`:\n```\n{result_preview}\n```")
                    except KeyError:
                        logger.error("GitHub config missing — set github.personal_access_token in settings.json")
                        await self._api.send_message(task.chat_id, "GitHub access failed: token missing.")
                    except Exception as e:
                        logger.warning("GitHub API failed for task=%d: %s", task.id, e)
                        await self._api.send_message(task.chat_id, f"GitHub error: {e}")

            for title, entry_type, tags, content in all_kb_saves:
                if title and content:
                    try:
                        conn = await db.get_conn()
                        entry_id = await knowledge_store.add(
                            conn, title=title, content=content,
                            type=entry_type, tags=tags, source="agent",
                            user_id=task.user_id,
                        )
                        await conn.close()
                        logger.info("KB_SAVE: created entry %d by agent for task=%d", entry_id, task.id)
                    except Exception as e:
                        logger.warning("KB_SAVE failed for task=%d: %s", task.id, e)

            for entry_id, title, entry_type, tags, content in all_kb_updates:
                try:
                    conn = await db.get_conn()
                    ok = await knowledge_store.update(
                        conn, entry_id,
                        title=title, content=content, type=entry_type, tags=tags,
                    )
                    await conn.close()
                    if ok:
                        logger.info("KB_UPDATE: entry %d updated by agent for task=%d", entry_id, task.id)
                    else:
                        logger.warning("KB_UPDATE: entry %d not found for task=%d", entry_id, task.id)
                except Exception as e:
                    logger.warning("KB_UPDATE failed for task=%d: %s", task.id, e)

            for cfg_path, cfg_value_json in all_config_updates:
                if cfg_path in _CONFIG_UPDATE_BLOCKED:
                    logger.warning("CONFIG_UPDATE: blocked sensitive path '%s' for task=%d", cfg_path, task.id)
                    await self._api.send_message(task.chat_id, f"⚠ CONFIG_UPDATE blocked: '{cfg_path}' is a protected key.")
                    continue
                try:
                    import json as _json
                    new_val = _json.loads(cfg_value_json)
                    from .config_store import save_config as _cfg_save
                    current = config.get()
                    # Navigate and patch dotted path
                    updated = {**current}
                    keys = cfg_path.split('.')
                    target = updated
                    for k in keys[:-1]:
                        if k not in target or not isinstance(target[k], dict):
                            target[k] = {}
                        target[k] = dict(target[k])
                        target = target[k]
                    target[keys[-1]] = new_val
                    conn = await db.init_config()
                    await _cfg_save(conn, updated)
                    await conn.close()
                    config.set(updated)
                    logger.info("CONFIG_UPDATE: set '%s' by agent for task=%d", cfg_path, task.id)
                except Exception as e:
                    logger.warning("CONFIG_UPDATE failed for task=%d: %s", task.id, e)
                    await self._api.send_message(task.chat_id, f"⚠ CONFIG_UPDATE '{cfg_path}' failed: {e}")

            for plugin_name, plugin_cfg in all_plugin_config_sets:
                try:
                    from .config_store import save_config as _cfg_save
                    current = config.get()
                    plugins = dict(current.get("plugins") or {})
                    plugins[plugin_name] = plugin_cfg
                    updated = {**current, "plugins": plugins}
                    conn = await db.init_config()
                    await _cfg_save(conn, updated)
                    await conn.close()
                    config.set(updated)
                    logger.info("PLUGIN_CONFIG_SET: '%s' saved by agent for task=%d", plugin_name, task.id)
                except Exception as e:
                    logger.warning("PLUGIN_CONFIG_SET failed for task=%d: %s", task.id, e)

            await self._conn.execute(
                """INSERT INTO bot_messages (telegram_message_id, chat_id, task_id, text, sent_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (sent["message_id"], task.chat_id, task.id, clean_result, int(time.time())),
            )
            await self._conn.commit()
        elif error:
            # Clean up any pending initial message on error
            init_msg_id = self._pending_initial_msgs.pop(task.id, None) if task.id else None
            if init_msg_id and task.id:
                try:
                    await self._conn.execute("DELETE FROM pending_initial_msgs WHERE task_id = ?", (task.id,))
                    await self._conn.commit()
                except Exception:
                    pass
            if "CLI_AUTH_REQUIRED" in error:
                err_text = "Claude CLI not logged in. Send /reauth to authenticate."
            else:
                err_text = None
                logger.debug("Agent error for task=%s (recovery will handle user notification): %s", task.id, error)
            if init_msg_id:
                notice = err_text or "⚠ Task failed — see logs."
                try:
                    await self._api.edit_message(task.chat_id, init_msg_id, notice)
                except Exception:
                    if err_text:
                        await self._api.send_message(task.chat_id, err_text)
            elif err_text:
                await self._api.send_message(task.chat_id, err_text)

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
                import json as _json
                self._mention_only_chats = set(_json.loads(row[0]))
                logger.info("Loaded %d mention-only chats", len(self._mention_only_chats))
        except Exception as e:
            logger.warning("Failed to load mention_only_chats: %s", e)

    async def _save_mention_only_chats(self) -> None:
        import json as _json
        await self._conn.execute(
            """INSERT OR REPLACE INTO daemon_state (key, value, updated_at) VALUES (?, ?, ?)""",
            ("mention_only_chats", _json.dumps(sorted(self._mention_only_chats)), int(time.time())),
        )
        await self._conn.commit()

    async def _load_chat_history(self, chat_id: int, limit: int = 30) -> list[dict]:
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
        """Handle a conversational message directly, bypassing kanban controller."""
        self._start_typing(chat_id)
        task_id: int | None = None
        try:
            agent = self._chat_agents.get(chat_id)
            if agent is None:
                persona = ""
                if self._coordinator and self._coordinator._chief:
                    persona = self._coordinator._chief.persona
                provider = self._coordinator._get_provider() if self._coordinator else None
                agent = GeneralistAgent(
                    task_id=0,
                    user_context={
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "background": self._user_backgrounds.get(user_id, ""),
                    },
                    provider=provider,
                    token_tracker=self._token_tracker,
                    persona=persona,
                    agent_class="chief",
                )
                history = await self._load_chat_history(chat_id)
                if history:
                    agent._messages = history
                    logger.info("Chat %d: restored %d history messages from DB", chat_id, len(history))
                self._chat_agents[chat_id] = agent
            # Insert real kanban task so it's visible in Web UI
            if self._board:
                proto = KanbanTask(id=None, chat_id=chat_id, user_id=user_id, content=content)
                task_id = await self._board.push_active(proto, agent_id="chat")
                if task_id:
                    self._chat_task_ids.add(task_id)
                    if reply_to_msg_id:
                        self._chat_reply_to[task_id] = reply_to_msg_id
            result = await asyncio.wait_for(agent.run(content), timeout=300.0)
            preliminary_msg_id: int | None = None
            for _ in range(5):
                clean, tool_feedback = await self._exec_tool_tags(result)
                if not tool_feedback:
                    result = clean
                    break
                # Send preliminary text while tool loop continues
                if preliminary_msg_id is None and clean.strip():
                    try:
                        init = await self._api.send_message(
                            chat_id,
                            _md_to_telegram_html(clean) + "\n\n<i>✎ working...</i>",
                            parse_mode="HTML",
                        )
                        preliminary_msg_id = init["message_id"]
                        if task_id:
                            self._pending_initial_msgs[task_id] = preliminary_msg_id
                    except Exception:
                        pass
                logger.info("Chat %d: tool results fed back, continuing", chat_id)
                result = await asyncio.wait_for(
                    agent.run(f"[Tool results]\n{tool_feedback}\n\nContinue with the task."),
                    timeout=300.0,
                )
            if task_id and self._board:
                await self._board.complete(task_id, result[:2000] if result else "")
            real_task = KanbanTask(id=task_id, chat_id=chat_id, user_id=user_id, content=content)
            await self._on_agent_result(real_task, result, None)
        except asyncio.TimeoutError:
            self._stop_typing(chat_id)
            if task_id and self._board:
                await self._board.fail(task_id, "timeout (300s)")
            await self._api.send_message(chat_id, "Timeout.")
        except Exception:
            self._stop_typing(chat_id)
            if task_id and self._board:
                try:
                    await self._board.fail(task_id, "exception in chat handler")
                except Exception:
                    pass
            logger.exception("Chat handler error for chat=%d", chat_id)

    async def _exec_tool_tags(self, result: str) -> "tuple[str, str | None]":
        """Execute read-only tool tags in result, return (cleaned_result, tool_output_or_None).

        Only GET GitHub calls and READ_EMAIL are auto-executed so the agent can process
        the data. Write operations and output tags (VOICE, MAP, SEND_EMAIL, BUTTONS) are
        left intact for _on_agent_result to handle after the tool loop ends.
        """
        import json as _json
        tool_results: list[str] = []

        while True:
            clean, github_args = _extract_github_api_tag(result)
            if not github_args:
                break
            method, endpoint, body = github_args
            if method != "GET":
                break  # leave write ops intact for _on_agent_result
            result = clean
            try:
                # Use httpx directly for reads — avoids gh CLI dependency
                github_cfg = config.section("github")
                token = github_cfg.get("token", "")
                url = f"https://api.github.com{endpoint}"
                headers = {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                async with httpx.AsyncClient(timeout=30.0) as hc:
                    resp = await hc.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    data_str = _json.dumps(data, ensure_ascii=False, indent=2)
                    tool_results.append(f"GitHub GET {endpoint}:\n{data_str[:4000]}")
                else:
                    tool_results.append(f"GitHub GET {endpoint}: HTTP {resp.status_code} — {resp.text[:200]}")
            except Exception as e:
                tool_results.append(f"GitHub GET {endpoint} failed: {e}")

        while True:
            clean, email_args = _extract_read_email_tag(result)
            if not email_args:
                break
            result = clean
            folder, count = email_args
            try:
                emails = await _read_emails(folder, count, config.section("email"))
                lines = [
                    f"{i+1}. From: {m['from']}\n   Subject: {m['subject']}\n   {m['date']}"
                    for i, m in enumerate(emails)
                ]
                tool_results.append(f"READ_EMAIL {folder} ({len(emails)} emails):\n" + "\n".join(lines))
            except Exception as e:
                tool_results.append(f"READ_EMAIL {folder} failed: {e}")

        clean, git_args = _extract_git_clone_tag(result)
        if git_args:
            repo_url, plugin_name = git_args
            result = clean
            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '', plugin_name)[:64]
            target = f"{_PLUGINS_DIR}/{safe_name}"
            try:
                os.makedirs(_PLUGINS_DIR, exist_ok=True)
                proc = await asyncio.create_subprocess_exec(
                    *_build_git_clone_cmd(repo_url, target),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
                if proc.returncode == 0:
                    tool_results.append(f"GIT_CLONE {repo_url} → {target}: success")
                else:
                    tool_results.append(f"GIT_CLONE {repo_url} → {target}: failed: {stderr.decode(errors='replace')[:300]}")
            except asyncio.TimeoutError:
                tool_results.append(f"GIT_CLONE {repo_url}: timeout (120s)")
            except Exception as e:
                tool_results.append(f"GIT_CLONE {repo_url}: error: {e}")

        while True:
            clean, plugin_name = _extract_plugin_config_get_tag(result)
            if not plugin_name:
                break
            result = clean
            plugins = config.get().get("plugins") or {}
            plugin_cfg = plugins.get(plugin_name) if isinstance(plugins, dict) else None
            import json as _json
            if plugin_cfg:
                tool_results.append(f"PLUGIN_CONFIG_GET '{plugin_name}':\n{_json.dumps(plugin_cfg, ensure_ascii=False, indent=2)}")
            else:
                tool_results.append(f"PLUGIN_CONFIG_GET '{plugin_name}': not configured (use PLUGIN_CONFIG_SET to initialize)")

        while True:
            clean, kb_query = _extract_kb_search_tag(result)
            if not kb_query:
                break
            result = clean
            try:
                conn = await db.get_conn()
                entries = await knowledge_store.search(conn, kb_query, limit=10)
                await conn.close()
                if entries:
                    lines = []
                    for e in entries:
                        tags = ", ".join(e.get("tags") or [])
                        tag_str = f" [{tags}]" if tags else ""
                        body = e["content"][:400]
                        if len(e["content"]) > 400:
                            body += "…"
                        lines.append(f"- ID:{e['id']} [{e['type']}]{tag_str} **{e['title']}**: {body}")
                    tool_results.append(f"KB_SEARCH '{kb_query}' ({len(entries)} results):\n" + "\n".join(lines))
                else:
                    tool_results.append(f"KB_SEARCH '{kb_query}': no results found")
            except Exception as e:
                tool_results.append(f"KB_SEARCH failed: {e}")

        result, found_restart = _extract_tor_restart_tag(result)
        if found_restart:
            status = await _restart_tor()
            tool_results.append(f"TOR_RESTART: {status}")

        return result, "\n\n".join(tool_results) if tool_results else None

    async def _do_git_clone(self, chat_id: int, repo_url: str, target: str) -> None:
        try:
            os.makedirs(_PLUGINS_DIR, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *_build_git_clone_cmd(repo_url, target),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
            if proc.returncode == 0:
                await self._api.send_message(chat_id, f"✓ Cloned to `{target}`")
            else:
                err = stderr.decode(errors="replace")[:300]
                await self._api.send_message(chat_id, f"Git clone failed:\n`{err}`")
        except asyncio.TimeoutError:
            await self._api.send_message(chat_id, "Git clone timed out (120s).")
        except Exception as e:
            logger.warning("Git clone error for %s: %s", repo_url, e)
            await self._api.send_message(chat_id, f"Git clone error: {e}")

    async def _notify_admin_new_user(self, telegram_id: int, name: str | None) -> None:
        cfg = config.section("users")
        tg_cfg = config.section("telegram")
        admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))
        for admin_id in admin_ids:
            await self._api.send_message(
                admin_id,
                f"New user requesting access: {name or 'unknown'} (ID: {telegram_id})\n/auth {telegram_id}",
            )

    def _flush_chat_queue(self, chat_id: int) -> None:
        """Spawn next queued chat message, if any."""
        queue = self._pending_chat_queue.get(chat_id, [])
        if not queue:
            self._pending_chat_queue.pop(chat_id, None)
            self._active_chat_content.pop(chat_id, None)
            return
        user_id, content, msg_id = queue.pop(0)
        if not queue:
            self._pending_chat_queue.pop(chat_id, None)
        self._active_chat_content[chat_id] = content
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
                has_data = (
                    stats.usage_pct is not None
                    or stats.tokens_used is not None
                    or stats.session_pct is not None
                )
                if has_data:
                    try:
                        import json as _json
                        first_model_pct = round(stats.weekly_models[0][1] * 100, 1) if stats.weekly_models else None
                        weekly_models_json = _json.dumps([{"name": n, "pct": round(p * 100, 1)} for n, p in stats.weekly_models]) if stats.weekly_models else None
                        await self._conn.execute(
                            """INSERT INTO usage_snapshots
                               (tokens_used, tokens_limit, usage_pct,
                                session_pct, weekly_all_pct, weekly_sonnet_pct,
                                session_reset_at, weekly_reset_at,
                                weekly_models_json, sampled_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                stats.tokens_used, stats.tokens_limit,
                                round(stats.usage_pct * 100, 1) if stats.usage_pct else None,
                                round(stats.session_pct * 100, 1) if stats.session_pct else None,
                                round(stats.weekly_all_pct * 100, 1) if stats.weekly_all_pct else None,
                                first_model_pct,
                                stats.session_reset_at, stats.weekly_reset_at,
                                weekly_models_json,
                                int(time.time()),
                            ),
                        )
                        await self._conn.commit()
                    except Exception:
                        pass
                if stats.is_near_limit and not self._usage_near_limit_notified:
                    self._usage_near_limit_notified = True
                    await self._notify_admins_usage(stats)
                elif not stats.is_near_limit:
                    self._usage_near_limit_notified = False
        except asyncio.CancelledError:
            pass

    _TOR_CHECK_INTERVAL = 60
    _TOR_TASK_COOLDOWN = 300  # don't re-push health task if one was pushed within this window

    async def _network_health_loop(self, admin_ids: list) -> None:
        """Periodically check if Tor is reachable; push a SECURITY task when it's down."""
        last_pushed: float = 0.0
        await asyncio.sleep(15.0)  # brief startup grace period
        try:
            while self._running:
                tor_ok = False
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", 9050), timeout=3.0
                    )
                    writer.close()
                    await writer.wait_closed()
                    tor_ok = True
                except Exception:
                    pass

                if not tor_ok:
                    now = time.time()
                    logger.warning("Network health: Tor SOCKS5 not reachable on 127.0.0.1:9050")
                    if self._board and admin_ids and (now - last_pushed) > self._TOR_TASK_COOLDOWN:
                        last_pushed = now
                        health_task = KanbanTask(
                            id=None,
                            chat_id=admin_ids[0],
                            user_id=admin_ids[0],
                            content=(
                                "## System Health Alert: Tor Not Reachable\n\n"
                                "Tor SOCKS5 proxy on 127.0.0.1:9050 is not responding.\n"
                                "Outbound traffic is unprotected until Tor is restored.\n\n"
                                "Action required:\n"
                                "1. Try restarting Tor with [TOR_RESTART]\n"
                                "2. Check the result and confirm Tor is up\n"
                                "3. Only notify the user if restart fails"
                            ),
                            agent_class=AgentClass.SECURITY,
                            priority=10,
                        )
                        await self._board.push(health_task)
                        logger.info("Pushed SECURITY task for Tor health failure")

                await asyncio.sleep(self._TOR_CHECK_INTERVAL)
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
