import asyncio
import logging
import time
from typing import Any

from . import config, db
from .agents.coordinator import AgentCoordinator
from .kanban.board import KanbanBoard
from .kanban.models import KanbanTask, Lane as _Lane
from .mode import DaemonMode
from .telegram.api import TelegramAPI
from .telegram.poller import TelegramPoller
from .telemetry.tokens import TokenTracker

logger = logging.getLogger(__name__)


async def reset_stale_tasks(daemon: Any) -> None:
    """Reset tasks interrupted by previous crash/restart so they're retried cleanly."""
    async with daemon._conn.execute("SELECT task_id, chat_id, tg_msg_id FROM pending_reactions") as cur:
        stale_reactions = await cur.fetchall()
    for _, chat_id, tg_msg_id in stale_reactions:
        try:
            await daemon._api.set_message_reaction(chat_id, tg_msg_id, None)
        except Exception:
            pass
    if stale_reactions:
        await daemon._conn.execute("DELETE FROM pending_reactions")
        await daemon._conn.commit()
        logger.info("Startup cleanup: cleared %d stale hourglass reactions", len(stale_reactions))

    async with daemon._conn.execute("SELECT task_id, chat_id, tg_msg_id FROM pending_initial_msgs") as cur:
        stale_initials = await cur.fetchall()
    for _, chat_id, tg_msg_id in stale_initials:
        try:
            await daemon._api.edit_message(chat_id, tg_msg_id, "↩ Restarted — task re-queued, working on it...")
        except Exception:
            pass
    if stale_initials:
        await daemon._conn.execute("DELETE FROM pending_initial_msgs")
        await daemon._conn.commit()
        logger.info("Startup cleanup: updated %d stale initial messages", len(stale_initials))

    stale_lanes = (_Lane.ASSIGNED.value, _Lane.IN_PROGRESS.value, _Lane.REVIEW.value)
    placeholders = ",".join("?" * len(stale_lanes))
    async with daemon._conn.execute(
        f"""UPDATE kanban_tasks SET lane = ?, agent_class = NULL, agent_id = NULL,
            started_at = NULL, assigned_at = NULL
            WHERE lane IN ({placeholders}) AND parent_id IS NULL""",
        (_Lane.BACKLOG.value, *stale_lanes),
    ) as cur:
        root_reset = cur.rowcount
    await daemon._conn.commit()
    async with daemon._conn.execute(
        f"DELETE FROM kanban_tasks WHERE lane IN ({placeholders}) AND parent_id IS NOT NULL",
        stale_lanes,
    ) as cur:
        children_removed = cur.rowcount
    await daemon._conn.commit()
    if root_reset or children_removed:
        logger.info(
            "Startup cleanup: %d root tasks → BACKLOG, %d orphaned children removed",
            root_reset, children_removed,
        )


async def init_run_components(daemon: Any) -> None:
    """Initialize all runtime components. Called in RUN mode."""
    daemon._mode_mgr.transition(DaemonMode.RUN)

    from .prompts import export_defaults as _export_prompts
    _export_prompts()

    daemon._conn = await db.init()

    from .knowledge.store import import_from_directory as _kb_import
    _imported = await _kb_import(daemon._conn)
    if _imported:
        logger.info("Imported %d knowledge file(s) from /data/knowledge/", _imported)

    tg_cfg = config.section("telegram")
    daemon._api = TelegramAPI(tg_cfg["token"])

    for _attempt in range(3):
        try:
            me = await daemon._api.get_me()
            daemon._bot_username = me.get("result", {}).get("username", "") or me.get("username", "")
            daemon._bot_id = me.get("result", {}).get("id", 0) or me.get("id", 0)
            logger.info("Bot username: @%s id=%d", daemon._bot_username, daemon._bot_id)
            break
        except Exception as e:
            logger.warning("getMe attempt %d failed: %s", _attempt + 1, e)
            if _attempt < 2:
                await asyncio.sleep(2 ** _attempt)
    else:
        logger.error("getMe failed after 3 attempts — bot will be silent in mention-only groups")

    await daemon._load_mention_only_chats()
    await daemon._load_muted_users()

    daemon._board = KanbanBoard(daemon._conn)
    daemon._token_tracker = TokenTracker(daemon._conn)
    await reset_stale_tasks(daemon)

    daemon._coordinator = AgentCoordinator(
        board=daemon._board,
        token_tracker=daemon._token_tracker,
        on_result=daemon._on_agent_result,
        on_requeue=daemon._on_task_requeued,
        user_backgrounds=daemon._user_backgrounds,
        exec_tools=daemon._exec_tool_tags,
        on_repair_trigger=daemon.trigger_repair,
    )
    daemon._coordinator.start()

    startup_ts = int(time.time())
    daemon._poller = TelegramPoller(daemon._api, daemon._on_update, skip_before_ts=startup_ts)
    daemon._poller.start()

    cfg = config.section("users")
    tg_cfg = config.section("telegram")
    admin_ids = cfg.get("admin_ids", tg_cfg.get("admin_chat_ids", []))

    if admin_ids:
        async with daemon._conn.execute(
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
            await daemon._board.push(migration_task)
            logger.info("Pushed KB classification task for %d untagged file-imported entries", unclassified)

    llm_cfg = config.section("llm")
    if llm_cfg.get("provider") == "cli":
        asyncio.create_task(daemon._check_cli_auth_on_startup(admin_ids), name="startup-auth-check")
    daemon._security.configure(daemon._api.send_message, admin_ids, log_fn=daemon._log_approval)

    asyncio.create_task(daemon._config_watcher_loop(), name="config-watcher")
    asyncio.create_task(daemon._usage_poll_loop(), name="usage-poller")
    asyncio.create_task(daemon._network_health_loop(admin_ids), name="network-health")
    asyncio.create_task(daemon._stuck_chat_watchdog(), name="stuck-chat-watchdog")

    from .cron import CronJob, CronManager
    from .tasks.deploy_watch import JOB_NAME as _DW_NAME, deploy_watch
    from .tasks.email_watch import JOB_NAME as _EW_NAME, email_watch
    from .tasks.kb_watch import JOB_NAME as _KBW_NAME, kb_watch

    async def _cron_notify(msg: str) -> None:
        for admin_id in admin_ids:
            try:
                await daemon._api.send_message(admin_id, msg)
            except Exception as e:
                logger.warning("Cron notification to admin %d failed: %s", admin_id, e)

    daemon._cron = CronManager(
        conn=daemon._conn,
        notify=_cron_notify,
        is_running=lambda: daemon._running,
    )
    daemon._cron.register(CronJob(
        name=_DW_NAME,
        handler=deploy_watch,
        default_interval_seconds=300,
        default_enabled=False,
    ))
    daemon._cron.register(CronJob(
        name=_EW_NAME,
        handler=email_watch,
        default_interval_seconds=3600,
        default_enabled=False,
    ))
    daemon._cron.register(CronJob(
        name=_KBW_NAME,
        handler=kb_watch,
        default_interval_seconds=21600,
        default_enabled=False,
    ))
    asyncio.create_task(daemon._cron.run(), name="cron-scheduler")
    asyncio.create_task(daemon._reminder_watcher(), name="reminder-watcher")

    daemon._running = True
    logger.info("claude-works daemon started in RUN mode")

    for admin_id in admin_ids:
        try:
            await daemon._api.send_message(admin_id, "✓ claude-works started and ready.")
        except Exception as e:
            logger.warning("Startup notification to admin %d failed: %s", admin_id, e)
