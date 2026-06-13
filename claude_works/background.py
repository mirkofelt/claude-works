import asyncio
import json
import logging
import time
from typing import Any

from . import config, db
from .config_store import load_config as _load_db_config
from .kanban.models import AgentClass, KanbanTask
from .tasks.reminders import fire_due_reminders as _fire_due_reminders

logger = logging.getLogger(__name__)

TYPING_INTERVAL = 4.0


async def typing_loop(daemon: Any, chat_id: int) -> None:
    try:
        while True:
            await daemon._api.send_chat_action(chat_id, "typing")
            await asyncio.sleep(TYPING_INTERVAL)
    except asyncio.CancelledError:
        pass


async def config_watcher_loop(daemon: Any) -> None:
    """Poll config.db every 5s; reload in-memory config when updated_at changes."""
    try:
        while daemon._running:
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
                        dropped = daemon._invalidate_stale_chat_agents()
                        logger.info("Config reloaded from DB (%d chat agent(s) refreshed)", dropped)
                await conn.close()
            except Exception as exc:
                logger.debug("Config watcher error: %s", exc)
    except asyncio.CancelledError:
        pass


async def usage_poll_loop(daemon: Any) -> None:
    try:
        while daemon._running:
            interval = config.section("llm").get("usage_poll_interval_seconds", 300)
            if interval <= 0:
                await asyncio.sleep(60.0)
                continue
            await asyncio.sleep(interval)
            if not daemon._coordinator:
                continue
            stats = await daemon._coordinator.query_usage()
            if stats is None:
                continue
            daemon._usage_state = stats
            pct_str = f"{stats.usage_pct * 100:.0f}%" if stats.usage_pct is not None else "?"
            logger.info("Claude Code usage: %s (tokens %s/%s)", pct_str, stats.tokens_used, stats.tokens_limit)
            has_data = (
                stats.usage_pct is not None
                or stats.tokens_used is not None
                or stats.session_pct is not None
            )
            if has_data:
                try:
                    first_model_pct = round(stats.weekly_models[0][1] * 100, 1) if stats.weekly_models else None
                    weekly_models_json = json.dumps([{"name": n, "pct": round(p * 100, 1)} for n, p in stats.weekly_models]) if stats.weekly_models else None
                    await daemon._conn.execute(
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
                    await daemon._conn.commit()
                except Exception:
                    pass
            if stats.is_near_limit and not daemon._usage_near_limit_notified:
                daemon._usage_near_limit_notified = True
                await notify_admins_usage(daemon, stats)
            elif not stats.is_near_limit:
                daemon._usage_near_limit_notified = False
    except asyncio.CancelledError:
        pass


async def stuck_chat_watchdog(daemon: Any) -> None:
    """Detect and clear chat handlers stuck > 10 minutes."""
    STUCK_THRESHOLD = 600  # seconds
    await asyncio.sleep(60.0)
    try:
        while daemon._running:
            await asyncio.sleep(60.0)
            now = time.time()
            for chat_id, task in list(daemon._typing_tasks.items()):
                if task.done():
                    daemon._typing_tasks.pop(chat_id, None)
                    daemon._flush_chat_queue(chat_id)
                    continue
            if daemon._chat_task_start_times:
                for chat_id, started_at in list(daemon._chat_task_start_times.items()):
                    if now - started_at > STUCK_THRESHOLD and chat_id in daemon._typing_tasks:
                        logger.warning("Stuck chat detected for chat=%d (%ds) — forcing cleanup", chat_id, int(now - started_at))
                        daemon._stop_typing(chat_id)
                        daemon._flush_chat_queue(chat_id)
                        daemon._chat_task_start_times.pop(chat_id, None)
                        try:
                            await daemon._api.send_message(chat_id, "⚠️ Previous request hung and was cancelled.")
                        except Exception:
                            pass
    except asyncio.CancelledError:
        pass


async def reminder_watcher(daemon: Any) -> None:
    """Fire due reminders every 30 seconds — no LLM, direct Telegram send."""
    await asyncio.sleep(10.0)
    try:
        while daemon._running:
            try:
                fired = await _fire_due_reminders(daemon._conn)
                for r in fired:
                    try:
                        markup = {
                            "inline_keyboard": [[
                                {"text": "✅ Erledigt", "callback_data": f"reminder_done:{r['id']}"},
                                {"text": "📋 Als Todo", "callback_data": f"reminder_todo:{r['id']}"},
                            ]]
                        }
                        await daemon._api.send_message(
                            r["chat_id"],
                            f"⏰ <b>Reminder #{r['id']}</b>\n{r['message']}",
                            parse_mode="HTML",
                            reply_markup=markup,
                        )
                        logger.info("Reminder %d fired for chat=%d", r["id"], r["chat_id"])
                    except Exception as e:
                        logger.warning("Reminder %d send failed: %s", r["id"], e)
            except Exception as e:
                logger.exception("Reminder watcher error: %s", e)
            await asyncio.sleep(30.0)
    except asyncio.CancelledError:
        pass


async def network_health_loop(daemon: Any, admin_ids: list) -> None:
    """Periodically check if Tor is reachable; push a SECURITY task when it's down."""
    last_pushed: float = 0.0
    await asyncio.sleep(15.0)  # brief startup grace period
    try:
        while daemon._running:
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
                if daemon._board and admin_ids and (now - last_pushed) > daemon._TOR_TASK_COOLDOWN:
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
                    await daemon._board.push(health_task)
                    logger.info("Pushed SECURITY task for Tor health failure")

            await asyncio.sleep(daemon._TOR_CHECK_INTERVAL)
    except asyncio.CancelledError:
        pass


async def notify_admins_usage(daemon: Any, stats) -> None:
    if not daemon._api:
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
            await daemon._api.send_message(admin_id, msg)
        except Exception:
            pass
