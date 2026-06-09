import asyncio
import json
import logging
import uuid

from ..config import get_agent_model, section
from ..kanban.board import KanbanBoard
from ..kanban.models import AgentClass
from ..llm.errors import RateLimitError
from ..llm.provider import LLMProvider, get_provider
from ..telemetry.tokens import TokenTracker

logger = logging.getLogger(__name__)

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


class ControllerAgent:
    """Routes tasks from backlog to the appropriate agent class."""

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
        self._running = False
        self._owns_provider = provider is None

    def _get_provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = get_provider(section("llm"))
        return self._provider

    async def _route(self, content: str) -> AgentClass:
        cfg = section("llm")
        model = get_agent_model("controller")

        response = await self._get_provider().complete(
            [{"role": "user", "content": content[:2000]}],
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
