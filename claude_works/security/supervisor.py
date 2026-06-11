import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .rules import Rule, build_rules, check_content
from ..config import section, get_agent_model
from ..prompts import load as _load_prompt

logger = logging.getLogger(__name__)


import re as _re


@dataclass
class PendingApproval:
    id: int
    task_id: int | None
    chat_id: int
    user_id: int
    action_types: list[str]
    content: str
    requested_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool = False
    decided_by: int | None = None
    specific_key: str | None = None   # e.g. "email_send:x@y.de"
    specific_label: str | None = None  # e.g. "an x@y.de"


class SecuritySupervisor:
    def __init__(self) -> None:
        self._rules: list[Rule] = []
        self._pending: dict[int, PendingApproval] = {}
        self._next_id = 1
        self._notify_fn: Any = None
        self._admin_ids: list[int] = []
        self._provider: Any = None
        self._always_allowed_actions: set[str] = set()
        self._skip_all: bool = False

    def configure(self, notify_fn: Any, admin_ids: list[int]) -> None:
        self._notify_fn = notify_fn
        self._admin_ids = admin_ids
        cfg = section("security")
        self._rules = build_rules(cfg.get("rules"))
        self._always_allowed_actions = set(cfg.get("always_allow_actions", []))
        self._skip_all = bool(cfg.get("skip_all", False))
        self._load_allowlist()
        logger.info(
            "SecuritySupervisor configured rules=%d enabled=%s always_allowed=%s skip_all=%s",
            len(self._rules), self.enabled, self._always_allowed_actions, self._skip_all,
        )

    def _config_db_path(self) -> str:
        return os.environ.get("CONFIG_DB_FILE", "/data/config.db")

    def _load_allowlist(self) -> None:
        try:
            with sqlite3.connect(self._config_db_path()) as conn:
                row = conn.execute(
                    "SELECT always_allowed_actions, skip_all FROM security_allowlist WHERE id = 1"
                ).fetchone()
            if row:
                self._always_allowed_actions.update(json.loads(row[0] or "[]"))
                if row[1]:
                    self._skip_all = True
        except sqlite3.OperationalError:
            pass  # table not yet created (first run)
        except Exception as e:
            logger.warning("Failed to load security allowlist from DB: %s", e)

    def _save_allowlist(self) -> None:
        try:
            with sqlite3.connect(self._config_db_path()) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO security_allowlist
                       (id, always_allowed_actions, skip_all, updated_at)
                       VALUES (1, ?, ?, ?)""",
                    (json.dumps(sorted(self._always_allowed_actions)), int(self._skip_all), int(time.time())),
                )
                conn.commit()
        except Exception as e:
            logger.warning("Failed to save security allowlist to DB: %s", e)

    def _get_provider(self):
        if self._provider is None:
            from ..llm.provider import get_provider
            self._provider = get_provider(section("llm"))
        return self._provider

    @property
    def enabled(self) -> bool:
        return section("security").get("enabled", False)

    @staticmethod
    def _extract_specific(action_type: str, content: str) -> "tuple[str, str] | None":
        """Extract (storage_key, button_label) for action-specific allowlist entries."""
        if action_type == "email_send":
            m = _re.search(r'\[SEND_EMAIL:\s*([^\s|\]]+)', content)
            if m:
                recipient = m.group(1).strip()
                return f"email_send:{recipient}", f"an {recipient[:30]}"
        elif action_type == "github_write":
            m = _re.search(r'\[GITHUB_API:\s*(POST|PUT|PATCH|DELETE)\s*\|\s*([^\s|\]\n]+)', content, _re.I)
            if m:
                method = m.group(1).upper()
                endpoint = m.group(2).strip()[:30]
                return f"github_write:{method}:{endpoint}", f"{method} {endpoint}"
        elif action_type == "tts_send":
            return f"tts_send:all", "vorlesen"
        return None

    @staticmethod
    def _action_label(action_type: str) -> str:
        return {
            "email_send": "✉️ Immer senden",
            "github_write": "🔧 Immer schreiben",
            "tts_send": "🔊 Immer vorlesen",
            "data_deletion": "🗑️ Immer löschen",
            "command_execution": "⚙️ Immer ausführen",
        }.get(action_type, f"🔄 Immer: {action_type[:18]}")

    def allow_action_type(self, action_type: str) -> None:
        self._always_allowed_actions.add(action_type)
        self._save_allowlist()
        logger.info("Security: '%s' permanently allowed", action_type)

    def allow_specific(self, specific_key: str) -> None:
        self._always_allowed_actions.add(specific_key)
        self._save_allowlist()
        logger.info("Security: specific key '%s' permanently allowed", specific_key)

    async def _run_so_check(self, action_type: str, content: str, task_id: int | None) -> bool:
        """LLM content review — always runs when security is enabled, regardless of allowlist."""
        try:
            model = get_agent_model("controller")
            prompt = f"Action: {action_type}\n\nContent to review:\n{content[:2000]}"
            response = await self._get_provider().complete(
                [{"role": "user", "content": prompt}],
                system=_load_prompt("security_officer"),
                model=model,
                max_tokens=128,
            )
            data = json.loads(response.text.strip())
            allowed = bool(data.get("allowed", False))
            if not allowed:
                reason = data.get("reason", "security review failed")
                logger.warning(
                    "Security officer blocked action=%s task=%s reason=%r",
                    action_type, task_id, reason,
                )
            else:
                logger.info("Security officer approved action=%s task=%s", action_type, task_id)
            return allowed
        except Exception as e:
            logger.error("Security officer review failed for action=%s: %s — blocking", action_type, e)
            return False

    async def check_action(
        self,
        action_type: str,
        content: str,
        task_id: int | None = None,
        chat_id: int = 0,
        user_id: int = 0,
    ) -> bool:
        """User allowlist controls whether approval is requested. SO content check always runs."""
        if not self.enabled:
            return True
        # SO always reviews content — allowlist/skip_all only bypass user approval prompts
        return await self._run_so_check(action_type, content, task_id)

    async def check(
        self,
        content: str,
        task_id: int | None = None,
        chat_id: int = 0,
        user_id: int = 0,
    ) -> bool:
        """Two-stage check: user approval (skippable via allowlist) + SO content review (always)."""
        if not self.enabled:
            return True

        triggered = check_content(content, self._rules)
        if not triggered:
            return True

        # Stage 1: user approval — skipped if pre-approved (action-type OR specific key)
        if not self._skip_all:
            need_approval = []
            for t in triggered:
                if t in self._always_allowed_actions:
                    continue
                specific = self._extract_specific(t, content)
                if specific and specific[0] in self._always_allowed_actions:
                    continue
                need_approval.append(t)
            if need_approval:
                user_ok = await self._request_approval(need_approval, content, task_id, chat_id, user_id)
                if not user_ok:
                    return False

        # Stage 2: SO content review — always runs regardless of allowlist
        return await self._run_so_check("response", content, task_id)

    async def _request_approval(
        self,
        action_types: list[str],
        content: str,
        task_id: int | None = None,
        chat_id: int = 0,
        user_id: int = 0,
    ) -> bool:
        approval_id = self._next_id
        self._next_id += 1

        specific_key, specific_label = None, None
        primary_type = action_types[0] if action_types else ""
        specific = self._extract_specific(primary_type, content)
        if specific:
            specific_key, specific_label = specific

        approval = PendingApproval(
            id=approval_id,
            task_id=task_id,
            chat_id=chat_id,
            user_id=user_id,
            action_types=action_types,
            content=content[:500],
            requested_at=time.time(),
            specific_key=specific_key,
            specific_label=specific_label,
        )
        self._pending[approval_id] = approval

        logger.warning(
            "Security check triggered id=%d task=%s actions=%s",
            approval_id, task_id, action_types,
        )

        await self._notify_admins(approval)

        timeout = section("security").get("pending_timeout_seconds", 300)
        try:
            await asyncio.wait_for(approval.event.wait(), timeout=float(timeout))
        except asyncio.TimeoutError:
            logger.warning("Approval id=%d timed out — auto-denying", approval_id)
            approval.approved = False

        self._pending.pop(approval_id, None)

        if approval.approved:
            logger.info("Approval id=%d approved by=%s", approval_id, approval.decided_by)
        else:
            logger.warning("Approval id=%d denied by=%s", approval_id, approval.decided_by)

        return approval.approved

    async def _notify_admins(self, approval: PendingApproval) -> None:
        if not self._notify_fn or not self._admin_ids:
            return
        types_str = ", ".join(approval.action_types)
        preview = approval.content[:200].replace("\n", " ")
        msg = (
            f"⚠️ Freigabe benötigt [#{approval.id}]\n"
            f"Aktion: {types_str}\n"
            f"Vorschau: {preview}"
        )
        primary_type = approval.action_types[0] if approval.action_types else ""
        row1 = [
            {"text": "✅ Einmalig", "callback_data": f"sec_once:{approval.id}"},
            {"text": self._action_label(primary_type), "callback_data": f"sec_always_action:{approval.id}"},
        ]
        row2 = [{"text": "❌ Ablehnen", "callback_data": f"sec_deny:{approval.id}"}]
        if approval.specific_key and approval.specific_label:
            row2.insert(0, {
                "text": f"🔁 Immer {approval.specific_label}",
                "callback_data": f"sec_always_specific:{approval.id}",
            })
        keyboard = {"inline_keyboard": [row1, row2]}
        for admin_id in self._admin_ids:
            try:
                await self._notify_fn(admin_id, msg, reply_markup=keyboard)
            except Exception as e:
                logger.error("Failed to notify admin %d: %s", admin_id, e)

    def approve(self, approval_id: int, admin_id: int) -> bool:
        approval = self._pending.get(approval_id)
        if not approval:
            return False
        approval.approved = True
        approval.decided_by = admin_id
        approval.event.set()
        return True

    def approve_always_specific(self, approval_id: int, admin_id: int) -> bool:
        approval = self._pending.get(approval_id)
        if not approval or not approval.specific_key:
            return False
        self.allow_specific(approval.specific_key)
        approval.approved = True
        approval.decided_by = admin_id
        approval.event.set()
        return True

    def approve_always_action(self, approval_id: int, admin_id: int) -> bool:
        approval = self._pending.get(approval_id)
        if not approval:
            return False
        for action_type in approval.action_types:
            self.allow_action_type(action_type)
        approval.approved = True
        approval.decided_by = admin_id
        approval.event.set()
        return True

    def deny(self, approval_id: int, admin_id: int) -> bool:
        approval = self._pending.get(approval_id)
        if not approval:
            return False
        approval.approved = False
        approval.decided_by = admin_id
        approval.event.set()
        return True

    def pending_list(self) -> list[dict]:
        now = time.time()
        return [
            {
                "id": a.id,
                "task_id": a.task_id,
                "chat_id": a.chat_id,
                "user_id": a.user_id,
                "action_types": a.action_types,
                "content": a.content,
                "requested_at": a.requested_at,
                "age_seconds": int(now - a.requested_at),
            }
            for a in self._pending.values()
        ]

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def always_allowed_actions(self) -> list[str]:
        return sorted(self._always_allowed_actions)

    @property
    def skip_all(self) -> bool:
        return self._skip_all
