import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .rules import Rule, build_rules, check_content
from ..config import section, get_agent_model
from ..prompts import load as _load_prompt

logger = logging.getLogger(__name__)

_OFFICER_SYSTEM = _load_prompt("security_officer")


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

    def configure(self, notify_fn: Any, admin_ids: list[int]) -> None:
        self._notify_fn = notify_fn
        self._admin_ids = admin_ids
        cfg = section("security")
        self._rules = build_rules(cfg.get("rules"))
        logger.info("SecuritySupervisor configured rules=%d enabled=%s", len(self._rules), self.enabled)

    def _get_provider(self):
        if self._provider is None:
            from ..llm.provider import get_provider
            self._provider = get_provider(section("llm"))
        return self._provider

    @property
    def enabled(self) -> bool:
        return section("security").get("enabled", False)

    async def check_action(
        self,
        action_type: str,
        content: str,
        task_id: int | None = None,
        chat_id: int = 0,
        user_id: int = 0,
    ) -> bool:
        """LLM-based content review for outbound actions (email, TTS, GitHub).
        Returns True if no information leak detected, False if blocked."""
        if not self.enabled:
            return True
        try:
            model = get_agent_model("controller")  # fast tier sufficient
            prompt = f"Action: {action_type}\n\nContent to review:\n{content[:2000]}"
            response = await self._get_provider().complete(
                [{"role": "user", "content": prompt}],
                system=_OFFICER_SYSTEM,
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
        """Returns True if content may proceed. False = blocked."""
        if not self.enabled:
            return True

        triggered = check_content(content, self._rules)
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
            f"⚠️ Security approval needed [#{approval.id}]\n"
            f"Triggers: {types_str}\n"
            f"Preview: {preview}\n\n"
            f"/approve {approval.id}  or  /deny {approval.id}"
        )
        for admin_id in self._admin_ids:
            try:
                await self._notify_fn(admin_id, msg)
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
