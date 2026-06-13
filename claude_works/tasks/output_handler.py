"""Execute write/output side-effects from a TagCollection after agent result is sent.

Handles TTS, maps, email, GitHub writes, KB saves/updates, config updates,
mutes, reminders, sub-task spawning, and orchestration.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone as _UTC
from typing import Any, TYPE_CHECKING

import httpx

from .. import config, db
from ..auth import trust as trust_mod
from ..auth.users import is_admin
from ..config_store import save_config as _save_db_config
from ..kanban.models import KanbanTask
from ..knowledge import store as knowledge_store
from ..security import whitelist as _whitelist
from ..tasks import tags as _tags
from ..tasks.email import send_email as _send_email
from ..tasks.github import github_api as _github_api
from ..tasks.reminders import parse_remind_at as _parse_remind_at, add_reminder as _add_reminder
from ..tasks.tts import synthesize as _synthesize_tts
from ..telegram.renderer import md_to_html as _md_to_telegram_html

if TYPE_CHECKING:
    from ..tasks.tags import TagCollection

logger = logging.getLogger(__name__)

_kb_write_allowed = _tags.kb_write_allowed
_CONFIG_UPDATE_BLOCKED = _tags.CONFIG_UPDATE_BLOCKED


def _user_error(context: str, exc: Exception | None = None) -> str:
    if exc is not None:
        logger.warning("%s: %s", context, exc)
    _FRIENDLY: dict[type, str] = {
        asyncio.TimeoutError: "Timed out.",
    }
    if exc is not None:
        for exc_type, msg in _FRIENDLY.items():
            if isinstance(exc, exc_type):
                return f"⚠️ {context} — {msg}"
    return f"⚠️ {context}."


async def execute_output_tags(
    daemon: Any, task: KanbanTask, tc: "TagCollection", sent_msg_id: int
) -> None:
    """Execute all write/output side-effects from a TagCollection.

    Called after the agent result is sent.
    """
    all_tts = tc.tts
    all_maps = tc.maps
    all_send_emails = tc.emails
    all_github = tc.github
    all_kb_saves = tc.kb_saves
    all_kb_updates = tc.kb_updates
    all_config_updates = tc.config_updates
    all_mutes = tc.mutes
    all_unmutes = tc.unmutes
    all_plugin_config_sets = tc.plugin_config_sets
    all_reminders = tc.reminders
    all_subtasks = tc.subtasks
    all_orchestrations = tc.orchestrations

    for tts_text in all_tts:
        tts_allowed = await daemon._security.check_action(
            "tts_send", tts_text, task_id=task.id, chat_id=task.chat_id, user_id=task.user_id
        )
        if tts_allowed:
            try:
                tts_cfg = config.section("tts")
                audio, tts_error = await _synthesize_tts(tts_text, tts_cfg)
                if audio:
                    await daemon._api.send_voice(task.chat_id, audio)
                elif tts_error:
                    logger.warning("TTS failed for task=%d: %s", task.id, tts_error)
                    await daemon._api.send_message(task.chat_id, _user_error("TTS failed"))
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
                await daemon._api.send_location(task.chat_id, lat, lon, title=title)
            else:
                await daemon._api.send_message(task.chat_id, f"📍 {map_query} — not found.")
        except Exception as e:
            logger.warning("Map geocoding failed for task=%d: %s", task.id, e)

    for to, subject, body in all_send_emails:
        email_content = f"To: {to}\nSubject: {subject}\n\n{body}"
        if daemon._security.whitelisted("send_email", _whitelist.email_context(to)):
            logger.info("Email to %s pre-approved by whitelist for task=%d", to, task.id)
            email_allowed = True
        else:
            email_allowed = await daemon._security.check_action(
                "email_send", email_content, task_id=task.id, chat_id=task.chat_id, user_id=task.user_id
            )
        if not email_allowed:
            logger.info("Email send blocked by security officer for task=%d", task.id)
            await daemon._api.send_message(task.chat_id, "Email blocked by security officer — possible data leak detected.")
        else:
            try:
                email_cfg = config.section("email")
                await _send_email(to, subject, body, email_cfg)
                await daemon._api.send_message(task.chat_id, f"✉️ Email sent to {to}.")
            except KeyError:
                logger.error("Email config missing — set email.smtp_host/user/password in settings.json")
                await daemon._api.send_message(task.chat_id, "Email not sent: email configuration missing.")
            except Exception as e:
                logger.warning("Email send failed for task=%d: %s", task.id, e)
                await daemon._api.send_message(task.chat_id, _user_error("Email send failed", e))

    for method, endpoint, body in all_github:
        is_write = method in ("POST", "PUT", "PATCH", "DELETE")
        do_exec = True
        if is_write:
            wl_type = _whitelist.classify_github(method, endpoint)
            wl_ctx = _whitelist.github_context(method, endpoint, body)
            if daemon._security.whitelisted(wl_type, wl_ctx):
                logger.info("GitHub %s %s pre-approved by whitelist (%s) for task=%d",
                            method, endpoint, wl_type, task.id)
            else:
                gh_content = f"{method} {endpoint}\n\n{body or ''}"
                gh_allowed = await daemon._security.check_action(
                    "github_write", gh_content, task_id=task.id, chat_id=task.chat_id, user_id=task.user_id
                )
                if not gh_allowed:
                    logger.info("GitHub write blocked by security officer for task=%d", task.id)
                    await daemon._api.send_message(task.chat_id, "GitHub write blocked by security officer — possible data leak detected.")
                    do_exec = False
        if do_exec:
            try:
                github_cfg = config.section("github")
                result_data = await _github_api(method, endpoint, body or None, github_cfg)
                result_preview = json.dumps(result_data, ensure_ascii=False, indent=2)[:1200]
                gh_msg = f"GitHub `{method} {endpoint}`:\n```\n{result_preview}\n```"
                try:
                    await daemon._api.send_message(task.chat_id, _md_to_telegram_html(gh_msg), parse_mode="HTML")
                except Exception:
                    await daemon._api.send_message(task.chat_id, gh_msg)
            except KeyError:
                logger.error("GitHub config missing — set github.personal_access_token in settings.json")
                await daemon._api.send_message(task.chat_id, "GitHub access failed: token missing.")
            except Exception as e:
                logger.warning("GitHub API failed for task=%d: %s", task.id, e)
                await daemon._api.send_message(task.chat_id, _user_error("GitHub action failed", e))

    for title, entry_type, tags, content in all_kb_saves:
        if title and content:
            try:
                conn = await db.get_conn()
                trust = await trust_mod.chat_trust(conn, task.chat_id, task.user_id)
                if task.chat_id is not None and task.chat_id < 0:
                    await conn.close()
                    logger.warning(
                        "KB_SAVE blocked: group chat=%s trust=%d task=%d — unverified source",
                        task.chat_id, trust, task.id,
                    )
                    continue
                if _kb_write_allowed(trust):
                    entry_id = await knowledge_store.add(
                        conn, title=title, content=content,
                        type=entry_type, tags=tags, source=f"chat:{task.chat_id}",
                        user_id=task.user_id,
                        visibility=trust_mod.VISIBILITY_PRIVATE,
                        origin_chat_id=task.chat_id,
                    )
                    await conn.close()
                    logger.info("KB_SAVE: created entry %d by agent for task=%d", entry_id, task.id)
                else:
                    entry_id = await knowledge_store.add(
                        conn, title=title, content=content,
                        type=entry_type, tags=tags, source=f"chat:{task.chat_id}",
                        user_id=task.user_id,
                        visibility=trust,
                        origin_chat_id=task.chat_id,
                        quarantined=1,
                    )
                    await conn.close()
                    logger.warning(
                        "KB_SAVE: entry %d quarantined (trust=%d, chat=%d, task=%d) — pending admin review",
                        entry_id, trust, task.chat_id, task.id,
                    )
                    await daemon._notify_admins_kb_quarantine(entry_id, title, task.chat_id, trust)
            except Exception as e:
                logger.warning("KB_SAVE failed for task=%d: %s", task.id, e)

    for entry_id, title, entry_type, tags, content in all_kb_updates:
        try:
            conn = await db.get_conn()
            trust = await trust_mod.chat_trust(conn, task.chat_id, task.user_id)
            if task.chat_id is not None and task.chat_id < 0:
                await conn.close()
                logger.warning(
                    "KB_UPDATE blocked: group chat=%s entry=%d trust=%d task=%d — unverified source",
                    task.chat_id, entry_id, trust, task.id,
                )
                continue
            if not _kb_write_allowed(trust):
                await conn.close()
                logger.warning("KB_UPDATE blocked: trust=%d (entry=%d, task=%d)", trust, entry_id, task.id)
                continue
            entry = await knowledge_store.get(conn, entry_id)
            if entry is not None and not trust_mod.can_see({"trust_level": trust}, entry):
                await conn.close()
                logger.warning("KB_UPDATE: entry %d hidden for trust=%d (task=%d) — blocked", entry_id, trust, task.id)
                continue
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
            await daemon._api.send_message(task.chat_id, f"⚠ CONFIG_UPDATE blocked: '{cfg_path}' is a protected key.")
            continue
        if daemon._security.whitelisted("config_put", _whitelist.config_context(cfg_path)):
            logger.info("CONFIG_UPDATE '%s' pre-approved by whitelist for task=%d", cfg_path, task.id)
        else:
            cfg_allowed = await daemon._security.check_action(
                "config_put", f"{cfg_path} = {cfg_value_json}",
                task_id=task.id, chat_id=task.chat_id, user_id=task.user_id,
            )
            if not cfg_allowed:
                logger.info("CONFIG_UPDATE '%s' blocked by security officer for task=%d", cfg_path, task.id)
                await daemon._api.send_message(task.chat_id, f"⚠ CONFIG_UPDATE '{cfg_path}' blocked by security officer.")
                continue
        try:
            new_val = json.loads(cfg_value_json)
            current = config.get()
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
            await _save_db_config(conn, updated)
            await conn.close()
            config.set(updated)
            logger.info("CONFIG_UPDATE: set '%s' by agent for task=%d", cfg_path, task.id)
        except Exception as e:
            logger.warning("CONFIG_UPDATE failed for task=%d: %s", task.id, e)
            await daemon._api.send_message(task.chat_id, _user_error(f"Config update failed: '{cfg_path}'", e))

    for ident, minutes in all_mutes:
        if not await is_admin(daemon._conn, task.user_id):
            logger.warning("MUTE tag from non-admin user=%d ignored (task=%d)", task.user_id, task.id)
            continue
        target = await daemon._resolve_user(ident)
        if not target:
            await daemon._api.send_message(task.chat_id, f"⚠ Mute failed: user '{ident}' not found.")
            continue
        if await is_admin(daemon._conn, target["telegram_id"]):
            await daemon._api.send_message(task.chat_id, "⚠ Admins cannot be muted.")
            continue
        until = await daemon._set_mute(target["telegram_id"], minutes)
        dur = f"for {minutes} min" if until else "indefinitely"
        await daemon._api.send_message(
            task.chat_id,
            f"🔇 Muted: {target.get('name') or target['telegram_id']} {dur}.",
        )

    for ident in all_unmutes:
        if not await is_admin(daemon._conn, task.user_id):
            logger.warning("UNMUTE tag from non-admin user=%d ignored (task=%d)", task.user_id, task.id)
            continue
        target = await daemon._resolve_user(ident)
        if target and await daemon._clear_mute(target["telegram_id"]):
            await daemon._api.send_message(
                task.chat_id, f"🔊 {target.get('name') or target['telegram_id']} unmuted."
            )

    for plugin_name, plugin_cfg in all_plugin_config_sets:
        try:
            current = config.get()
            plugins = dict(current.get("plugins") or {})
            plugins[plugin_name] = plugin_cfg
            updated = {**current, "plugins": plugins}
            conn = await db.init_config()
            await _save_db_config(conn, updated)
            await conn.close()
            config.set(updated)
            logger.info("PLUGIN_CONFIG_SET: '%s' saved by agent for task=%d", plugin_name, task.id)
        except Exception as e:
            logger.warning("PLUGIN_CONFIG_SET failed for task=%d: %s", task.id, e)

    await daemon._conn.execute(
        """INSERT INTO bot_messages (telegram_message_id, chat_id, task_id, text, sent_at)
           VALUES (?, ?, ?, ?, ?)""",
        (sent_msg_id, task.chat_id, task.id, tc.clean_result, int(time.time())),
    )
    await daemon._conn.commit()

    for dt_str, message in all_reminders:
        remind_at = _parse_remind_at(dt_str)
        if remind_at is None:
            await daemon._api.send_message(
                task.chat_id,
                f"⚠️ Reminder could not be set — time not recognised: `{dt_str}`\n"
                "Formats: `YYYY-MM-DD HH:MM`, `HH:MM`, `+30m`, `+2h`, `+1d`",
            )
        else:
            reminder_id = await _add_reminder(daemon._conn, task.user_id, task.chat_id, remind_at, message)
            dt_readable = datetime.fromtimestamp(remind_at, tz=_UTC.utc).strftime("%d.%m.%Y %H:%M UTC")
            await daemon._api.send_message(
                task.chat_id,
                f"⏰ Reminder #{reminder_id} set for **{dt_readable}**:\n_{message}_",
            )
            logger.info("Reminder %d set for %s by user=%d", reminder_id, dt_readable, task.user_id)

    for sub_desc in all_subtasks:
        if daemon._board:
            sub_proto = KanbanTask(id=None, chat_id=task.chat_id, user_id=task.user_id,
                                  content=sub_desc, parent_id=task.id)
            await daemon._board.push(sub_proto)
            logger.info("Sub-task spawned by task=%d: %s", task.id, sub_desc[:80])

    for project_name, task_descs in all_orchestrations:
        if daemon._board:
            spawned = []
            for desc in task_descs:
                full_desc = f"[Project: {project_name}] {desc}"
                sub_proto = KanbanTask(id=None, chat_id=task.chat_id, user_id=task.user_id,
                                      content=full_desc, parent_id=task.id)
                await daemon._board.push(sub_proto)
                spawned.append(desc[:60])
            logger.info("Orchestrator task=%d spawned %d sub-tasks for project '%s'",
                        task.id, len(spawned), project_name)
            lines = "\n".join(f"• {s}" for s in spawned)
            await daemon._api.send_message(
                task.chat_id,
                f"🔀 Project **{project_name}** — {len(spawned)} tasks gestartet:\n{lines}",
            )
