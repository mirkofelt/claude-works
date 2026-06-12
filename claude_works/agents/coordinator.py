import asyncio
import logging
import time

from ..config import agent_timeout, section, get as _get_config
from .. import db
from ..auth import trust as trust_mod
from ..knowledge import store as knowledge_store
from ..kanban.board import KanbanBoard
from ..kanban.models import AgentClass, KanbanTask
from ..llm.errors import RateLimitError
from ..llm.provider import LLMProvider, get_provider
from ..telemetry.task_log import TaskLogger
from ..telemetry.tokens import BudgetExceededError, TokenTracker
from .chief import ChiefAgent
from .controller import ControllerAgent
from .heartbeat import run_with_heartbeat
from .po import ProductOwnerAgent
from .specialist.generalist import GeneralistAgent
from .specialist.researcher import ResearchAgent
from .specialist.code_team import CodeTeam
from .specialist.memory import MemoryAgent
from .specialist.security import SecurityAgent

logger = logging.getLogger(__name__)


_KB_MIN_WORDS = 4
_KB_ENTRY_MAX_CHARS = 400
_CODETEAM_PREVIEW_LEN = 100


async def _inject_knowledge(content: str, user_id: int | None, chat_id: int | None = None) -> str:
    """Prepend relevant knowledge base entries to task content for agent context.

    Filtert nach Vertrauensstufe: nur Einträge mit visibility >= effektiver
    Stufe des Chats (Gruppen: lockerste Stufe aller Mitglieder)."""
    if len(content.split()) < _KB_MIN_WORDS:
        return content
    try:
        conn = await db.get_conn()
        trust = await trust_mod.chat_trust(conn, chat_id, user_id)
        entries = await knowledge_store.search(conn, content, user_id=user_id, limit=5, trust=trust)
        await conn.close()
    except Exception:
        return content
    if not entries:
        return content
    lines = []
    for e in entries:
        tags = ", ".join(e.get("tags") or [])
        tag_str = f" [{tags}]" if tags else ""
        body = e["content"][:_KB_ENTRY_MAX_CHARS]
        if len(e["content"]) > _KB_ENTRY_MAX_CHARS:
            body += "…"
        lines.append(f"- ID:{e['id']} [{e['type']}]{tag_str} **{e['title']}**: {body}")
    kb_block = "## Relevant knowledge\n(use KB_UPDATE:<id> to update, KB_SAVE to add new)\n" + "\n".join(lines)
    return f"{kb_block}\n\n---\n\n{content}"


_SPECIALIST_MAP = {
    AgentClass.GENERALIST: GeneralistAgent,
    AgentClass.RESEARCHER: ResearchAgent,
    AgentClass.CODER: CodeTeam,
    AgentClass.MEMORY: MemoryAgent,
    AgentClass.SECURITY: SecurityAgent,
}


class AgentCoordinator:
    """Orchestrates controller, chief, and specialist workers over KanbanBoard."""

    def __init__(self, board: KanbanBoard, token_tracker: TokenTracker, on_result, on_requeue=None, user_backgrounds: dict | None = None, exec_tools=None, on_repair_trigger=None) -> None:
        self._board = board
        self._token_tracker = token_tracker
        self._on_result = on_result
        self._on_requeue = on_requeue
        self._user_backgrounds: dict[int, str] = user_backgrounds or {}
        self._exec_tools = exec_tools
        self._on_repair_trigger = on_repair_trigger  # async (result: str) -> tuple[str, str | None]
        self._provider: LLMProvider | None = None
        self._controller: ControllerAgent | None = None
        self._chief: ChiefAgent | None = None
        self._po: ProductOwnerAgent | None = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._active: dict[str, asyncio.Task] = {}
        self._rate_limit_until: float = 0.0
        self._rate_limit_count: int = 0

    def _get_provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = get_provider(section("llm"))
        return self._provider

    def start(self) -> None:
        self._running = True
        provider = self._get_provider()

        self._controller = ControllerAgent(
            board=self._board,
            provider=provider,
            token_tracker=self._token_tracker,
            on_result=self._on_result,
            on_repair_trigger=self._on_repair_trigger,
        )
        self._chief = ChiefAgent(
            board=self._board,
            provider=provider,
            token_tracker=self._token_tracker,
        )
        self._po = ProductOwnerAgent(
            board=self._board,
            provider=provider,
            token_tracker=self._token_tracker,
        )

        self._tasks.append(asyncio.create_task(self._controller.run_loop(), name="controller-loop"))
        self._tasks.append(asyncio.create_task(self._chief.run_loop(self._on_result), name="chief-loop"))
        self._tasks.append(asyncio.create_task(self._po.run_loop(self._on_result), name="po-loop"))

        for agent_class in (AgentClass.GENERALIST, AgentClass.RESEARCHER, AgentClass.CODER, AgentClass.MEMORY, AgentClass.SECURITY):
            self._tasks.append(asyncio.create_task(
                self._specialist_loop(agent_class),
                name=f"specialist-{agent_class.value}",
            ))

        logger.info("AgentCoordinator started (%d loops)", len(self._tasks))

    async def stop(self) -> None:
        self._running = False
        if self._controller:
            self._controller.stop()
        if self._chief:
            self._chief.stop()
        if self._po:
            self._po.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        active_snapshot = list(self._active.values())
        for t in active_snapshot:
            t.cancel()
        await asyncio.gather(*active_snapshot, return_exceptions=True)
        self._active.clear()
        if self._provider:
            await self._provider.close()
        logger.info("AgentCoordinator stopped")

    def cancel_task(self, task_id: int) -> bool:
        """Cancel a running agent task by task_id. Returns True if found and cancelled."""
        for key, asyncio_task in list(self._active.items()):
            if key.endswith(f"-{task_id}"):
                asyncio_task.cancel()
                self._active.pop(key, None)
                logger.info("Coordinator: cancelled active task %d (key=%s)", task_id, key)
                return True
        return False

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def is_running(self) -> bool:
        return self._running

    async def query_usage(self):
        if self._provider:
            return await self._provider.query_usage()
        return None

    @property
    def is_rate_limited(self) -> bool:
        return time.time() < self._rate_limit_until

    @property
    def rate_limit_until(self) -> float | None:
        return self._rate_limit_until if self.is_rate_limited else None

    async def _specialist_loop(self, agent_class: AgentClass) -> None:
        cfg = section("agents")
        max_parallel = cfg.get("max_parallel", 4)
        logger.info("Specialist loop started for %s", agent_class.value)

        while self._running:
            # Pause during rate limit cooldown
            now = time.time()
            if now < self._rate_limit_until:
                await asyncio.sleep(min(self._rate_limit_until - now, 30.0))
                continue

            running_for_class = sum(
                1 for k in self._active if k.startswith(agent_class.value)
            )
            if running_for_class >= max_parallel:
                await asyncio.sleep(0.5)
                continue

            task = await self._board.next_assigned(agent_class)
            if task is None:
                await asyncio.sleep(1.0)
                continue

            key = f"{agent_class.value}-{task.id}"
            active_task = asyncio.create_task(
                self._run_specialist(task, agent_class),
                name=f"specialist-run-{key}",
            )
            self._active[key] = active_task
            active_task.add_done_callback(lambda _, k=key: self._active.pop(k, None))

    async def _run_specialist(self, task: KanbanTask, agent_class: AgentClass) -> None:
        idle_timeout = agent_timeout("idle_timeout_seconds")
        max_runtime = agent_timeout("max_runtime_seconds")
        AgentCls = _SPECIALIST_MAP[agent_class]
        persona = self._chief.persona if self._chief else ""

        tlog = TaskLogger(task.id)
        tlog.info(f"task {task.id} assigned to {agent_class.value}")
        agent = AgentCls(
            task_id=task.id,
            user_context={
                "user_id": task.user_id,
                "chat_id": task.chat_id,
                "background": self._user_backgrounds.get(task.user_id, ""),
            },
            provider=self._get_provider(),
            token_tracker=self._token_tracker,
            persona=persona,
        )
        # Token attribution: CodeTeam keeps its own 'coderteam' source; every other
        # board-spawned specialist is a 'background' job. run_id stays the agent id so
        # all API calls of this run (incl. tool-loop iterations) group together.
        if agent_class != AgentClass.CODER:
            agent.source = "background"
        agent_run_id = f"{agent_class.value}-{agent.id}"
        started = time.time()

        try:
            started = await self._board.start(task.id, agent_run_id)
            if not started:
                logger.warning("Specialist %s task %d already claimed — skipping", agent_class.value, task.id)
                return

            if not task.content or not task.content.strip():
                logger.warning("Specialist: task %d has empty content — failing immediately", task.id)
                await self._board.fail(task.id, "Empty task content")
                return

            content = await _inject_knowledge(task.content, task.user_id, task.chat_id)
            sys_mode = _get_config().get("system", {}).get("mode", "run")
            if sys_mode != "run":
                content = f"[SYSTEM MODE: {sys_mode.upper()}]\n\n{content}"
            # Hard cap spans the whole task incl. tool loop; idle timeout only
            # kills a run with no LLM/tool activity (heartbeat supervision).
            deadline = time.monotonic() + max_runtime
            result = await run_with_heartbeat(
                agent.run(content), agent.heartbeat, idle_timeout, deadline=deadline,
            )
            # Tool loop — feed read-tool results back so agent can continue processing
            if self._exec_tools:
                for _ in range(5):
                    clean, tool_feedback = await self._exec_tools(result, user_id=task.user_id, chat_id=task.chat_id)
                    agent.heartbeat.beat()  # tool execution is progress
                    if not tool_feedback:
                        result = clean
                        break
                    logger.info("Specialist %s task %d: tool results fed back, continuing", agent_class.value, task.id)
                    result = await run_with_heartbeat(
                        agent.run(
                            f"[Tool results]\n{tool_feedback}\n\n"
                            "Process the tool results above and continue with the task. "
                            "Do NOT echo or repeat raw tool output — summarise in plain language only."
                        ),
                        agent.heartbeat, idle_timeout, deadline=deadline,
                    )
            self._rate_limit_count = 0  # reset on success
            elapsed = time.time() - started
            logger.info("Specialist %s task %d done in %.1fs", agent_class.value, task.id, elapsed)
            tlog.info(f"task {task.id} done in {elapsed:.1f}s")
            await self._board.complete(task.id, result)
            await self._on_result(task, result, None)
        except asyncio.TimeoutError as exc:
            err = f"Task aborted by supervisor: {exc or f'idle > {idle_timeout:.0f}s'}"
            logger.error("Specialist %s task %d timed out: %s", agent_class.value, task.id, exc)
            tlog.error(err)
            await self._board.fail(task.id, err)
            await self._on_result(task, None, err)
        except RateLimitError as exc:
            self._rate_limit_count += 1
            base = exc.retry_after or 30.0
            cooldown = min(base * (2 ** (self._rate_limit_count - 1)), 900.0)
            self._rate_limit_until = time.time() + cooldown
            logger.warning(
                "Rate limited (hit #%d); cooldown %.0fs; requeuing task %d",
                self._rate_limit_count, cooldown, task.id,
            )
            tlog.warn(f"rate limited — requeued (cooldown {cooldown:.0f}s)")
            await self._board.requeue(task.id)
            if self._on_requeue:
                await self._on_requeue(task)
        except BudgetExceededError as exc:
            err = str(exc)
            logger.warning("Budget exceeded for task %d: %s", task.id, err)
            tlog.error(f"budget exceeded: {err}")
            await self._board.fail(task.id, err)
            await self._on_result(task, None, err)
        except Exception as exc:
            logger.exception("Specialist %s task %d failed", agent_class.value, task.id)
            tlog.error(f"failed: {exc}")
            await self._board.fail(task.id, str(exc))
            await self._on_result(task, None, str(exc))
