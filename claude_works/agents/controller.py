import asyncio
import json
import logging
import re
import uuid

from ..config import get_agent_model, section
from ..kanban.board import KanbanBoard
from ..kanban.models import AgentClass
from ..llm.errors import RateLimitError
from ..llm.provider import LLMProvider, get_provider
from ..telemetry.tokens import TokenTracker

logger = logging.getLogger(__name__)

_FAST_ROUTES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^/?(status|ping|help)\b", re.I), "generalist"),
    (re.compile(r"^/?(reload_config|reload_persona|repair)\b", re.I), "generalist"),
    (re.compile(r"^\s*(remember|store|save)\s+", re.I), "memory"),
    (re.compile(r"^\s*(forget|delete from (memory|kb)|remove from (memory|kb))\b", re.I), "memory"),
    (re.compile(r"^\s*(search|find|lookup|retrieve)\s+(memory|kb|knowledge)\b", re.I), "memory"),
]
_FAST_ROUTE_MAX_LEN = 200


def _fast_route(content: str) -> "AgentClass | None":
    if len(content.strip()) > _FAST_ROUTE_MAX_LEN:
        return None
    stripped = content.strip()
    for pattern, agent_class_val in _FAST_ROUTES:
        if pattern.match(stripped):
            try:
                return AgentClass(agent_class_val)
            except ValueError:
                pass
    return None


_ROUTING_SYSTEM = """You are a task router. Given a task, respond ONLY with valid JSON:
{"agent_class": "<class>", "reason": "<brief reason>"}

Classes:
- "generalist": conversation, general questions, analysis, simple requests
- "researcher": research, fact-finding, information lookup
- "coder": code writing, debugging, reviews (runs full Architect→Dev→Test→QA pipeline)
- "memory": knowledge base operations (store/retrieve/manage)
- "chief": strategic decisions, persona-sensitive tasks, high-priority
- "po": complex multi-step projects requiring planning and decomposition, autonomous long-running tasks

Use "po" when the task is clearly multi-faceted and benefits from being split into parallel subtasks.
For simple single-step requests, prefer the direct specialist class.

No other text. Valid JSON only."""

_RECOVERY_SYSTEM = """You are a task recovery router. A task failed — decide recovery action.

Respond ONLY with valid JSON:
{"action": "<action>", "agent_class": "<class>", "reason": "<brief>"}

Actions:
- "retry": same agent, retry as-is (transient error, rate limit, timeout)
- "reroute": different agent class (wrong specialization caused the failure)
- "enrich": same agent, but prepend failure context to help it avoid the same mistake
- "abandon": unrecoverable (permission error, budget exceeded, malformed request)

Agent classes: generalist, researcher, coder, memory, chief

No other text. Valid JSON only."""

_MAX_RECOVERY_ATTEMPTS = 2


class ControllerAgent:
    """Routes tasks from backlog to the appropriate agent class.

    Also watches the FAILED lane and proactively attempts recovery:
    re-routing, enriching with error context, or retrying up to
    _MAX_RECOVERY_ATTEMPTS times before giving up.
    """

    def __init__(
        self,
        board: KanbanBoard,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
        on_result=None,
    ) -> None:
        self.id = str(uuid.uuid4())[:8]
        self._board = board
        self._provider = provider
        self._token_tracker = token_tracker
        self._on_result = on_result
        self._running = False
        self._owns_provider = provider is None
        self._recovery_attempts: dict[int, int] = {}  # task_id → attempt count
        self._exhausted: set[int] = set()  # task_ids that hit max retries

    def _get_provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = get_provider(section("llm"))
        return self._provider

    async def _decide_recovery(self, content: str, error: str) -> "tuple[str, AgentClass]":
        """Ask LLM how to recover a failed task. Returns (action, agent_class)."""
        model = get_agent_model("controller")
        prompt = f"Task:\n{content[:400]}\n\nError:\n{error[:200]}"
        response = await self._get_provider().complete(
            [{"role": "user", "content": prompt}],
            system=_RECOVERY_SYSTEM,
            model=model,
            max_tokens=128,
        )
        if self._token_tracker:
            await self._token_tracker.log(
                agent_id=self.id,
                agent_class="controller",
                task_id=None,
                user_id=None,
                chat_id=None,
                model=model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_write_tokens=response.usage.cache_write_tokens,
            )
        try:
            data = json.loads(response.text.strip())
            action = data.get("action", "retry")
            agent_class = AgentClass(data.get("agent_class", "generalist"))
            logger.info(
                "Recovery decision task error=%r → action=%s class=%s reason=%r",
                error[:60], action, agent_class.value, data.get("reason", ""),
            )
            return action, agent_class
        except (json.JSONDecodeError, ValueError):
            logger.warning("Bad recovery response: %r — defaulting retry/generalist", response.text)
            return "retry", AgentClass.GENERALIST

    async def _handle_recovery(self, task: "KanbanTask") -> None:
        from ..kanban.models import KanbanTask as _KanbanTask  # avoid circular at module level
        task_id = task.id
        attempts = self._recovery_attempts.get(task_id, 0) + 1
        self._recovery_attempts[task_id] = attempts

        if attempts > _MAX_RECOVERY_ATTEMPTS:
            self._exhausted.add(task_id)
            logger.warning("Recovery exhausted for task %d after %d attempts", task_id, attempts - 1)
            if self._on_result:
                fake = _KanbanTask(
                    id=task_id, chat_id=task.chat_id, user_id=task.user_id,
                    content=task.content,
                )
                await self._on_result(fake, None, f"Konnte nicht behoben werden: {task.error}")
            return

        error = task.error or "unknown error"
        logger.info("Recovery attempt %d/%d for task %d error=%r", attempts, _MAX_RECOVERY_ATTEMPTS, task_id, error[:60])

        try:
            action, agent_class = await self._decide_recovery(task.content, error)
        except Exception:
            logger.exception("Recovery routing failed for task %d — defaulting retry", task_id)
            action, agent_class = "retry", AgentClass.GENERALIST

        if action == "abandon":
            self._exhausted.add(task_id)
            logger.warning("Recovery: abandon task %d", task_id)
            if self._on_result:
                from ..kanban.models import KanbanTask as _KT
                fake = _KT(id=task_id, chat_id=task.chat_id, user_id=task.user_id, content=task.content)
                await self._on_result(fake, None, f"Aufgabe abgebrochen: {error}")
            return

        if action == "enrich":
            new_content = f"[Vorheriger Versuch fehlgeschlagen: {error}]\n\n{task.content}"
        else:
            new_content = None

        recovered = await self._board.recover(task_id, content=new_content)
        if recovered:
            if action == "reroute":
                assigned = await self._board.assign(task_id, agent_class)
                if not assigned:
                    logger.warning("Recovery reroute assign failed for task %d", task_id)
            logger.info("Task %d recovered action=%s class=%s", task_id, action, agent_class.value)

    async def _route(self, content: str) -> AgentClass:
        fast = _fast_route(content)
        if fast is not None:
            logger.info("Controller fast-route: %r → %s", content[:60], fast.value)
            return fast

        model = get_agent_model("controller")

        response = await self._get_provider().complete(
            [{"role": "user", "content": content[:500]}],
            system=_ROUTING_SYSTEM,
            model=model,
            max_tokens=128,
        )

        if self._token_tracker:
            await self._token_tracker.log(
                agent_id=self.id,
                agent_class="controller",
                task_id=None,
                user_id=None,
                chat_id=None,
                model=model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_write_tokens=response.usage.cache_write_tokens,
            )

        try:
            data = json.loads(response.text.strip())
            agent_class = AgentClass(data.get("agent_class", "generalist"))
            logger.info(
                "Controller route task_preview=%r → %s reason=%r",
                content[:60], agent_class.value, data.get("reason", ""),
            )
            return agent_class
        except (json.JSONDecodeError, ValueError):
            logger.warning("Controller bad routing response: %r — defaulting to generalist", response.text)
            return AgentClass.GENERALIST

    async def run_loop(self) -> None:
        self._running = True
        logger.info("Controller %s loop started", self.id)
        while self._running:
            task = await self._board.next_backlog()
            if task is None:
                # No backlog work — check failed tasks for recovery
                failed = await self._board.next_failed(exclude_ids=self._exhausted)
                if failed:
                    try:
                        await self._handle_recovery(failed)
                    except Exception:
                        logger.exception("Recovery handler error for task %d", failed.id)
                        self._exhausted.add(failed.id)
                else:
                    await self._board.wait_for_work(timeout=10.0)
                continue
            try:
                agent_class = await self._route(task.content)
                assigned = await self._board.assign(task.id, agent_class)
                if not assigned:
                    logger.warning("Controller could not assign task %d (already moved?)", task.id)
            except RateLimitError as exc:
                wait = exc.retry_after or 30.0
                logger.warning(
                    "Controller rate limited; task %d stays in backlog; pausing %.0fs",
                    task.id, wait,
                )
                await asyncio.sleep(wait)
            except Exception:
                logger.exception("Controller error routing task %d", task.id)
                await self._board.fail(task.id, "routing error")

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        self._running = False
        if self._owns_provider and self._provider:
            await self._provider.close()
