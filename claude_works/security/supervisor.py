import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .rules import Rule, build_rules, check_content
from ..config import section, get_agent_model
from ..prompts import load as _load_prompt

logger = logging.getLogger(__name__)

_ALLOWLIST_FILE = os.environ.get("SECURITY_ALLOWLIST_FILE", "/data/security_allowlist.json")


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

    def _load_allowlist(self) -> None:
        try:
            with open(_ALLOWLIST_FILE) as f:
                data = json.load(f)
            self._always_allowed_actions.update(data.get("always_allowed_actions", []))
            if data.get("skip_all"):
                self._skip_all = True
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Failed to load security allowlist: %s", e)

    def _save_allowlist(self) -> None:
        try:
            with open(_ALLOWLIST_FILE, "w") as f:
                json.dump({
                    "always_allowed_actions": sorted(self._always_allowed_actions),
                    "skip_all": self._skip_all,
                }, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save security allowlist: %s", e)

    def _get_provider(self):
        if self._provider is None:
            from ..llm.provider import get_provider
            self._provider = get_provider(section("llm"))
        return self._provider

    @property
    def enabled(self) -> bool:
        return section("security").get("enabled", False)

    def allow_action_type(self, action_type: str) -> None:
        self._always_allowed_actions.add(action_type)
        self._save_allowlist()
        logger.info("Security: '%s' permanently allowed", action_type)

    def allow_all(self) -> None:
        self._skip_all = True
        self._save_allowlist()
        logger.warning("Security: all checks permanently disabled by admin")

    async def check_action(
        self,
        action_type: str,
        content: str,
        task_id: int | None = None,
        chat_id: int = 0,
        user_id: int = 0,
    ) -> bool:
        if not self.enabled:
            return True
        if self._skip_all or action_type in self._always_allowed_actions:
            return True
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

    async def check(
        self,
        content: str,
        task_id: int | None = None,
        chat_id: int = 0,
        user_id: int = 0,
    ) -> bool:
        if not self.enabled or self._skip_all:
            return True

        triggered = check_content(content, self._rules)
        if not triggered:
            return True
        triggered = [t for t in triggered if t not in self._always_allowed_actions]
        if not triggered:
            return True
        return await self._request_approval(triggered, content, task_id, chat_id, user_id)

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

        approval = PendingApproval(
            id=approval_id,
            task_id=task_id,
            chat_id=chat_id,
            user_id=user_id,
            action_types=action_types,
            content=content[:500],
            requested_at=time.time(),
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
        # Truncate action label for button (max ~20 chars to fit Telegram button)
        action_label = types_str[:20]
        keyboard = {"inline_keyboard": [
            [
                {"text": "✅ Einmalig", "callback_data": f"sec_once:{approval.id}"},
                {"text": f"🔄 Immer: {action_label}", "callback_data": f"sec_always_action:{approval.id}"},
            ],
            [
                {"text": "🔓 Immer alles", "callback_data": f"sec_always_all:{approval.id}"},
                {"text": "❌ Ablehnen", "callback_data": f"sec_deny:{approval.id}"},
            ],
        ]}
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

    def approve_always_all(self, approval_id: int, admin_id: int) -> bool:
        approval = self._pending.get(approval_id)
        if not approval:
            return False
        self.allow_all()
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
