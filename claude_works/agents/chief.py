import asyncio
import logging
import os
import uuid

from ..config import section
from ..kanban.board import KanbanBoard
from ..kanban.models import AgentClass
from ..knowledge import store as knowledge_store
from ..llm.errors import RateLimitError
from ..llm.provider import LLMProvider, get_provider
from ..telemetry.tokens import TokenTracker
from .concepts import get_system_prompt, get_caveman_addendum

logger = logging.getLogger(__name__)

_DEFAULT_PERSONA_FILE = "/data/persona.md"

_CHIEF_ADDENDUM = """

## Chief Role
You are the top-level coordinator. Handle strategic decisions, high-priority tasks,
and anything requiring the full persona. You have access to the knowledge base.
Delegate clearly when appropriate.
"""


class ChiefAgent:
    """Persona holder, knowledge base owner, chief task handler."""

    def __init__(
        self,
        board: KanbanBoard,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
    ) -> None:
        self.id = str(uuid.uuid4())[:8]
        self._board = board
        self._provider = provider
        self._token_tracker = token_tracker
        self._persona: str = ""
        self._running = False
        self._owns_provider = provider is None

    def _get_provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = get_provider(section("llm"))
        return self._provider

    @property
    def persona(self) -> str:
        return self._persona

    def load_persona(self) -> None:
        path = os.environ.get("PERSONA_FILE", _DEFAULT_PERSONA_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                self._persona = f.read().strip()
            logger.info("Chief persona loaded from %s (%d chars)", path, len(self._persona))
        except FileNotFoundError:
            logger.info("No persona file at %s — using default system prompt", path)
            self._persona = ""

    def reload_persona(self) -> None:
        self.load_persona()

    def _system_prompt(self, user_context: dict) -> str:
        base = self._persona or get_system_prompt()
        base += _CHIEF_ADDENDUM
        if user_context.get("caveman_mode", True):
            base += get_caveman_addendum()
        return base

    async def _run_task(self, task, on_result) -> None:
        from .specialist.generalist import GeneralistAgent
        agent = GeneralistAgent(
            task_id=task.id,
            user_context={"user_id": task.user_id, "chat_id": task.chat_id},
            provider=self._get_provider(),
            token_tracker=self._token_tracker,
            persona=self._system_prompt({"user_id": task.user_id, "chat_id": task.chat_id}),
            agent_class="chief",
        )
        cfg = section("agents")
        timeout = cfg.get("task_timeout_seconds", 600)
        agent_id = f"chief-{self.id}"
        try:
            await self._board.start(task.id, agent_id)
            result = await asyncio.wait_for(agent.run(task.content), timeout=timeout)
            await self._board.complete(task.id, result)
            await on_result(task, result, None)
        except asyncio.TimeoutError:
            err = f"Task timed out after {timeout}s"
            await self._board.fail(task.id, err)
            await on_result(task, None, err)
        except RateLimitError as exc:
            logger.warning("Chief rate limited task %d; requeuing (retry_after=%.0fs)", task.id, exc.retry_after or 30.0)
            await self._board.requeue(task.id)
        except Exception as exc:
            logger.exception("Chief task %d failed", task.id)
            await self._board.fail(task.id, str(exc))
            await on_result(task, None, str(exc))
        finally:
            # provider shared — do not close here
            pass

    async def run_loop(self, on_result) -> None:
        self._running = True
        self.load_persona()
        logger.info("Chief %s loop started", self.id)
        while self._running:
            task = await self._board.next_assigned(AgentClass.CHIEF)
            if task is None:
                await asyncio.sleep(2.0)
                continue
            asyncio.create_task(
                self._run_task(task, on_result),
                name=f"chief-task-{task.id}",
            )

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        self._running = False
        if self._owns_provider and self._provider:
            await self._provider.close()
