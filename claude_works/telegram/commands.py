import asyncio
import logging
import time
from datetime import datetime, timezone as _UTC
from typing import Any

from .. import config, db
from ..config_store import load_config as _load_db_config
from ..mode import DaemonMode
from ..auth.users import is_admin, is_allowed, set_role, set_trust
from ..auth import trust as trust_mod
from ..knowledge import store as knowledge_store
from ..tasks.reminders import (
    add_reminder as _add_reminder,
    delete_reminder as _delete_reminder,
    list_reminders as _list_reminders,
    parse_remind_at as _parse_remind_at,
)

logger = logging.getLogger(__name__)


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


async def handle_command(daemon: Any, text: str, from_id: int, chat_id: int) -> None:
    parts = text.strip().split()
    raw_cmd = parts[0].lower()
    if "@" in raw_cmd:
        raw_cmd = raw_cmd.split("@")[0]
    cmd = raw_cmd

    if cmd == "/auth" and len(parts) >= 2:
        if not await is_admin(daemon._conn, from_id):
            await daemon._api.send_message(chat_id, "Nope.")
            return
        try:
            target_id = int(parts[1].lstrip("@"))
            await set_role(daemon._conn, target_id, "user")
            await daemon._api.send_message(chat_id, f"User {target_id} approved.")
        except Exception as e:
            await daemon._api.send_message(chat_id, _user_error("Aktion fehlgeschlagen", e))

    elif cmd == "/block" and len(parts) >= 2:
        if not await is_admin(daemon._conn, from_id):
            return
        try:
            target_id = int(parts[1].lstrip("@"))
            await set_role(daemon._conn, target_id, "blocked")
            await daemon._api.send_message(chat_id, f"User {target_id} blocked.")
        except Exception as e:
            await daemon._api.send_message(chat_id, _user_error("Aktion fehlgeschlagen", e))

    elif cmd == "/approve" and len(parts) >= 2:
        if not await is_admin(daemon._conn, from_id):
            return
        try:
            ok = daemon._security.approve(int(parts[1]), from_id)
            await daemon._api.send_message(chat_id, f"✓ Approved #{parts[1]}" if ok else f"No pending approval #{parts[1]}")
        except Exception as e:
            await daemon._api.send_message(chat_id, _user_error("Aktion fehlgeschlagen", e))

    elif cmd == "/deny" and len(parts) >= 2:
        if not await is_admin(daemon._conn, from_id):
            return
        try:
            ok = daemon._security.deny(int(parts[1]), from_id)
            await daemon._api.send_message(chat_id, f"✗ Denied #{parts[1]}" if ok else f"No pending approval #{parts[1]}")
        except Exception as e:
            await daemon._api.send_message(chat_id, _user_error("Aktion fehlgeschlagen", e))

    elif cmd == "/trust":
        if not await is_admin(daemon._conn, from_id):
            await daemon._api.send_message(chat_id, "Nur für Admins.")
            return
        if len(parts) < 3:
            await daemon._api.send_message(
                chat_id,
                "Usage: /trust <telegram_id> <stufe>\n0=Owner 1=Vertraut 2=Kontakt 3=Unbekannt",
            )
            return
        try:
            target_id = int(parts[1].lstrip("@"))
            level = int(parts[2])
            ok = await set_trust(daemon._conn, target_id, level)
            if ok:
                label = trust_mod.TRUST_LABELS.get(level, str(level))
                await daemon._api.send_message(chat_id, f"User {target_id} → Stufe {level} ({label}).")
            else:
                await daemon._api.send_message(chat_id, f"User {target_id} unbekannt.")
        except Exception as e:
            await daemon._api.send_message(chat_id, _user_error("Aktion fehlgeschlagen", e))

    elif cmd == "/kb-level":
        if not await is_admin(daemon._conn, from_id):
            await daemon._api.send_message(chat_id, "Nur für Admins.")
            return
        if len(parts) < 3:
            await daemon._api.send_message(
                chat_id,
                "Usage: /kb-level <eintrag_id> <stufe>\n0=privat 1=vertraut 2=Kontakte 3=öffentlich",
            )
            return
        try:
            entry_id = int(parts[1])
            level = int(parts[2])
            if level not in (0, 1, 2, 3):
                await daemon._api.send_message(chat_id, "Stufe muss 0–3 sein.")
                return
            conn = await db.get_conn()
            ok = await knowledge_store.update(conn, entry_id, visibility=level)
            await conn.close()
            if ok:
                label = trust_mod.VISIBILITY_LABELS.get(level, str(level))
                await daemon._api.send_message(chat_id, f"KB-Eintrag {entry_id} → {label} ({level}).")
            else:
                await daemon._api.send_message(chat_id, f"KB-Eintrag {entry_id} nicht gefunden.")
        except Exception as e:
            await daemon._api.send_message(chat_id, _user_error("Aktion fehlgeschlagen", e))

    elif cmd == "/status":
        h = daemon.health()
        mode_info = f" | mode: {h['mode']}"
        sec = f" | sec: {h['security_pending']} pending" if h.get('security_pending') else ""
        msg = f"poller: {'✓' if h['poller'] else '✗'} | agents: {h['active_agents']} active{mode_info}{sec}"
        await daemon._api.send_message(chat_id, msg)

    elif cmd == "/getwebauth":
        if not await is_admin(daemon._conn, from_id):
            await daemon._api.send_message(chat_id, "Nope.")
            return
        token = config.section("web").get("auth_token", "")
        if token:
            await daemon._api.send_message(chat_id, f"`{token}`", parse_mode="Markdown")
        else:
            await daemon._api.send_message(chat_id, "web.auth_token not configured.")

    elif cmd == "/reload_persona":
        if not await is_admin(daemon._conn, from_id):
            return
        if daemon._coordinator and daemon._coordinator._chief:
            daemon._coordinator._chief.reload_persona()
            await daemon._api.send_message(chat_id, "Persona reloaded.")
        else:
            await daemon._api.send_message(chat_id, "Chief not running.")

    elif cmd == "/reload_config":
        if not await is_admin(daemon._conn, from_id):
            return
        try:
            conn = await db.init_config()
            cfg = await _load_db_config(conn)
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
                dropped = daemon._invalidate_stale_chat_agents()
                await daemon._api.send_message(
                    chat_id,
                    f"Config reloaded from DB. {dropped} chat agent(s) refreshed.",
                )
                logger.info("Config reloaded via /reload_config by user=%d", from_id)
            else:
                await daemon._api.send_message(chat_id, "No config found in DB.")
        except Exception as e:
            await daemon._api.send_message(chat_id, _user_error("Reload fehlgeschlagen", e))

    elif cmd == "/mention":
        if not await is_allowed(daemon._conn, from_id):
            return
        arg = parts[1].lower() if len(parts) >= 2 else ""
        if arg == "on":
            daemon._mention_only_chats.add(chat_id)
            await daemon._save_mention_only_chats()
            await daemon._api.send_message(chat_id, "👂 Mention-only mode active — responding only when @mentioned.")
        elif arg == "off":
            daemon._mention_only_chats.discard(chat_id)
            await daemon._save_mention_only_chats()
            await daemon._api.send_message(chat_id, "💬 Now responding to all messages.")
        else:
            state = "on" if chat_id in daemon._mention_only_chats else "off"
            await daemon._api.send_message(chat_id, f"Mention-only mode: {state}\nUsage: /mention on|off")

    elif cmd == "/mute":
        if not await is_admin(daemon._conn, from_id):
            await daemon._api.send_message(chat_id, "Nur für Admins.")
            return
        if len(parts) < 2:
            await daemon._api.send_message(chat_id, "Usage: /mute <name|telegram_id> [minuten]\nOhne Minuten: unbegrenzt.")
            return
        target = await daemon._resolve_user(parts[1])
        if not target:
            await daemon._api.send_message(chat_id, f"User '{parts[1]}' nicht gefunden.")
            return
        if await is_admin(daemon._conn, target["telegram_id"]):
            await daemon._api.send_message(chat_id, "Admins können nicht gemutet werden.")
            return
        try:
            minutes = int(parts[2]) if len(parts) >= 3 else 0
        except ValueError:
            minutes = 0
        until = await daemon._set_mute(target["telegram_id"], minutes)
        dur = f"für {minutes} min" if until else "unbegrenzt"
        await daemon._api.send_message(
            chat_id,
            f"🔇 {target.get('name') or target['telegram_id']} stumm {dur}. Nachrichten werden still mitgelesen.\nAufheben: /unmute {parts[1]}",
        )

    elif cmd == "/unmute":
        if not await is_admin(daemon._conn, from_id):
            await daemon._api.send_message(chat_id, "Nur für Admins.")
            return
        if len(parts) < 2:
            await daemon._api.send_message(chat_id, "Usage: /unmute <name|telegram_id>")
            return
        target = await daemon._resolve_user(parts[1])
        if not target:
            await daemon._api.send_message(chat_id, f"User '{parts[1]}' nicht gefunden.")
            return
        if await daemon._clear_mute(target["telegram_id"]):
            await daemon._api.send_message(chat_id, f"🔊 {target.get('name') or target['telegram_id']} wieder freigegeben.")
        else:
            await daemon._api.send_message(chat_id, f"{target.get('name') or target['telegram_id']} war nicht gemutet.")

    elif cmd == "/muted":
        if not await is_admin(daemon._conn, from_id):
            return
        if not daemon._muted_users:
            await daemon._api.send_message(chat_id, "Niemand gemutet.")
            return
        lines = []
        for tid, until in daemon._muted_users.items():
            u = await daemon._resolve_user(str(tid))
            name = (u.get("name") if u else None) or str(tid)
            if until == 0:
                lines.append(f"🔇 {name} — unbegrenzt")
            else:
                remaining = max(0, until - int(time.time())) // 60
                lines.append(f"🔇 {name} — noch ~{remaining} min")
        await daemon._api.send_message(chat_id, "\n".join(lines))

    elif cmd == "/repair" and len(parts) >= 2:
        if not await is_admin(daemon._conn, from_id):
            return
        error = " ".join(parts[1:])
        await daemon.trigger_repair(error)
        await daemon._api.send_message(chat_id, "Repair mode activated. Mechanic spawned.")

    elif cmd == "/exit_repair":
        if not await is_admin(daemon._conn, from_id):
            return
        if daemon._mode_mgr.mode not in (DaemonMode.REPAIR, DaemonMode.MIGRATE):
            await daemon._api.send_message(chat_id, "Not in repair/migrate mode.")
            return
        await daemon.exit_repair()
        await daemon._api.send_message(chat_id, "Exited repair mode. Normal operation resumed.")

    elif cmd == "/reauth":
        if not await is_admin(daemon._conn, from_id):
            await daemon._api.send_message(chat_id, "Nur für Admins.")
            return
        await daemon._start_telegram_reauth(chat_id)
        return

    elif cmd == "/reminders":
        reminders = await _list_reminders(daemon._conn, from_id)
        if not reminders:
            await daemon._api.send_message(chat_id, "Keine ausstehenden Erinnerungen.")
        else:
            lines = []
            for r in reminders:
                dt = datetime.fromtimestamp(r["remind_at"], tz=_UTC.utc).strftime("%d.%m.%Y %H:%M UTC")
                lines.append(f"#{r['id']} {dt} — {r['message'][:60]}")
            await daemon._api.send_message(chat_id, "⏰ Erinnerungen:\n" + "\n".join(lines))
        return

    elif cmd == "/remind_cancel" and len(parts) >= 2:
        try:
            reminder_id = int(parts[1])
        except ValueError:
            await daemon._api.send_message(chat_id, "Usage: /remind_cancel <id>")
            return
        deleted = await _delete_reminder(daemon._conn, reminder_id, from_id)
        if deleted:
            await daemon._api.send_message(chat_id, f"✓ Erinnerung #{reminder_id} gelöscht.")
        else:
            await daemon._api.send_message(chat_id, f"Erinnerung #{reminder_id} nicht gefunden oder bereits ausgelöst.")
        return

    elif cmd == "/remind":
        # /remind <time> <message>
        # time formats: +30m, +2h, +1d, HH:MM, DD.MM.YYYY HH:MM, YYYY-MM-DD HH:MM
        if len(parts) < 3:
            await daemon._api.send_message(
                chat_id,
                "Usage: /remind <zeit> <nachricht>\n"
                "Zeit: +30m · +2h · +1d · 14:30 · 13.06.2026 15:00 · 2026-06-13 15:00\n"
                "Beispiel: /remind +1h Anruf zurückrufen",
            )
            return
        # Try 1, 2, then 3 leading tokens as time expression.
        # Stops at the first successful parse, rest is the message.
        remind_at = None
        msg_start = 2
        for n_tokens in range(1, min(4, len(parts))):
            candidate = " ".join(parts[1:1 + n_tokens])
            ts = _parse_remind_at(candidate)
            if ts is not None:
                remind_at = ts
                msg_start = 1 + n_tokens
                break
        if remind_at is None or msg_start >= len(parts):
            await daemon._api.send_message(
                chat_id,
                "Zeitformat nicht erkannt oder fehlende Nachricht.\n"
                "Beispiele: /remind +30m Text · /remind morgen 15:00 Text · /remind 13.06.2026 15:00 Text",
            )
            return
        message = " ".join(parts[msg_start:])
        reminder_id = await _add_reminder(daemon._conn, from_id, chat_id, remind_at, message)
        dt = datetime.fromtimestamp(remind_at, tz=_UTC.utc).strftime("%d.%m. %H:%M UTC")
        await daemon._api.send_message(chat_id, f"⏰ Erinnerung #{reminder_id} gesetzt für {dt}: {message[:60]}")
        return

    elif cmd in ("/redeploy", "/deploy"):
        if not await is_admin(daemon._conn, from_id):
            await daemon._api.send_message(chat_id, "Nur für Admins.")
            return
        await daemon._api.send_message(chat_id, "🚀 Redeploy gestartet…")
        try:
            from ..tasks.deploy_watch import _trigger_deploy
            await _trigger_deploy()
        except Exception as e:
            await daemon._api.send_message(chat_id, f"⚠️ Redeploy fehlgeschlagen: {e}")
        return

    elif cmd == "/rollback":
        if not await is_admin(daemon._conn, from_id):
            await daemon._api.send_message(chat_id, "Nur für Admins.")
            return
        await daemon._api.send_message(chat_id, "⏪ Rollback gestartet…")
        try:
            import httpx as _httpx
            dg = config.section("system").get("claude_guard", {})
            guard_url = dg.get("url", "").rstrip("/")
            token = dg.get("token", "")
            if not guard_url or not token:
                raise RuntimeError("claude_guard.url/.token nicht konfiguriert")
            async with _httpx.AsyncClient(timeout=30.0) as hc:
                r = await hc.post(f"{guard_url}/rollback?token={token}")
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            await daemon._api.send_message(chat_id, f"⚠️ Rollback fehlgeschlagen: {e}")
        return

    elif cmd == "/help":
        help_text = (
            "<b>Verfügbare Befehle</b>\n\n"
            "<b>Erinnerungen</b>\n"
            "/remind +30m|14:30|13.06.2026 15:00 &lt;nachricht&gt; — Erinnerung setzen\n"
            "/reminders — Ausstehende Erinnerungen\n"
            "/remind_cancel &lt;id&gt; — Erinnerung löschen\n\n"
            "<b>System</b>\n"
            "/status — Daemon-Status\n"
            "/redeploy — Neuestes Image deployen\n"
            "/rollback — Auf vorheriges Image zurück\n"
            "/repair &lt;fehler&gt; — Repair-Modus aktivieren\n"
            "/exit_repair — Repair-Modus beenden\n\n"
            "<b>Chat</b>\n"
            "/mention on|off — Nur auf @Mentions reagieren\n\n"
            "<b>Admin</b>\n"
            "/auth /block /trust /mute /unmute /muted\n"
            "/approve /deny /reload_config /reload_persona"
        )
        await daemon._api.send_message(chat_id, help_text, parse_mode="HTML")
        return
