import asyncio
import json
import logging
import uuid

from ..config import get_agent_model, section
from ..kanban.board import KanbanBoard
from ..kanban.models import AgentClass, KanbanTask
from ..llm.errors import RateLimitError
from ..llm.provider import LLMProvider, get_provider
from ..telemetry.tokens import BudgetExceededError, TokenTracker

logger = logging.getLogger(__name__)

from ..prompts import load as _load_prompt

_DECOMPOSE_SYSTEM = _load_prompt("po_decompose")
_SYNTHESIZE_SYSTEM = _load_prompt("po_synthesize")


class ProductOwnerAgent:
    """Decomposes high-level tasks into subtasks, tracks completion, synthesizes results.

    Lifecycle: ASSIGNED → IN_PROGRESS (decompose) → REVIEW (waiting children) → DONE/FAILED
    """

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

    async def _llm(self, messages: list[dict], system: str, max_tokens: int = 512, model: str | None = None) -> str:
        attempt = 0
        while True:
            model = model or get_agent_model("po")
            if self._token_tracker:
                allowed = await self._token_tracker.get_allowed_model(model)
                if allowed is None:
                    raise BudgetExceededError("Spending limit reached — PO task rejected")
                model = allowed
            try:
                response = await self._get_provider().complete(
                    messages, system=system, model=model, max_tokens=max_tokens
                )
            except RateLimitError as exc:
                attempt += 1
                wait = min((exc.retry_after or 30.0) * (2 ** (attempt - 1)), 900.0)
                logger.warning("PO rate limited; retry in %.0fs (attempt %d)", wait, attempt)
                await asyncio.sleep(wait)
                continue
            if self._token_tracker:
                await self._token_tracker.log(
                    agent_id=self.id,
                    agent_class="po",
                    task_id=None,
                    user_id=None,
                    chat_id=None,
                    model=model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    cache_read_tokens=response.usage.cache_read_tokens,
                    cache_write_tokens=response.usage.cache_write_tokens,
                )
            return response.text

    async def _decompose(self, task: KanbanTask) -> list[dict]:
        text = await self._llm(
            [{"role": "user", "content": task.content[:4000]}],
            system=_DECOMPOSE_SYSTEM,
            model=get_agent_model("controller"),  # fast tier — routing doesn't need Sonnet
        )
        try:
            items = json.loads(text.strip())
            if not isinstance(items, list) or not items:
                raise ValueError("empty")
            valid = {c.value for c in AgentClass}
            for item in items:
                if not isinstance(item.get("description"), str):
                    item["description"] = item.get("title", task.content)
                if item.get("agent_class") not in valid:
                    item["agent_class"] = "generalist"
            return items[:8]
        except (json.JSONDecodeError, ValueError):
            logger.warning("PO decompose parse error for task=%d: %r", task.id, text[:120])
            return [{"title": "Execute", "description": task.content, "agent_class": "generalist"}]

    _MAX_CHILD_RESULT_CHARS = 4800  # ~1200 tokens

    async def _synthesize(self, goal: str, children: list[KanbanTask]) -> str:
        parts = []
        for c in children:
            label = (c.content or "")[:80]
            if c.result:
                result_text = c.result[:self._MAX_CHILD_RESULT_CHARS]
                if len(c.result) > self._MAX_CHILD_RESULT_CHARS:
                    result_text += "\n[…truncated]"
                parts.append(f"### {label}\n{result_text}")
            elif c.error:
                parts.append(f"### {label}\n[FAILED: {c.error}]")
        results_text = "\n\n".join(parts) or "(no results)"
        return await self._llm(
            [{"role": "user", "content": f"## Goal\n{goal}\n\n## Subtask Results\n{results_text}"}],
            system=_SYNTHESIZE_SYSTEM,
            max_tokens=2048,
        )

    async def _handle_project(self, task: KanbanTask, on_result) -> None:
        cfg = section("agents")
        timeout = cfg.get("po_timeout_seconds", 3600)
        agent_id = f"po-{self.id}"

        try:
            await self._board.start(task.id, agent_id)

            subtasks_spec = await self._decompose(task)
            logger.info("PO task=%d → %d subtasks", task.id, len(subtasks_spec))

            child_ids = []
            for st in subtasks_spec:
                child = KanbanTask(
                    id=None,
                    chat_id=task.chat_id,
                    user_id=task.user_id,
                    content=st["description"],
                    priority=task.priority,
                    parent_id=task.id,
                )
                agent_class = AgentClass(st.get("agent_class", "generalist"))
                child_id = await self._board.push_child(child, agent_class)
                child_ids.append(child_id)

            await self._board.review(task.id)
            logger.info("PO task=%d waiting for children %s", task.id, child_ids)

            children = await asyncio.wait_for(
                self._board.await_children(task.id, child_ids),
                timeout=timeout,
            )

            result = await self._synthesize(task.content, children)
            await self._board.complete(task.id, result)
            await on_result(task, result, None)

        except asyncio.TimeoutError:
            err = f"Project timed out after {timeout}s"
            logger.error("PO task=%d timed out", task.id)
            await self._board.fail(task.id, err)
            await on_result(task, None, err)
        except Exception as exc:
            logger.exception("PO task=%d failed", task.id)
            await self._board.fail(task.id, str(exc))
            await on_result(task, None, str(exc))

    async def run_loop(self, on_result) -> None:
        self._running = True
        logger.info("PO %s loop started", self.id)
        while self._running:
            task = await self._board.next_assigned(AgentClass.PO)
            if task is None:
                await asyncio.sleep(2.0)
                continue
            asyncio.create_task(
                self._handle_project(task, on_result),
                name=f"po-project-{task.id}",
            )

    def stop(self) -> None:
        self._running = False

    async def close(self) -> None:
        self._running = False
        if self._owns_provider and self._provider:
            await self._provider.close()
