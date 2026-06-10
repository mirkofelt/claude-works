import logging
from enum import Enum

from .base import BaseAgent
from ..prompts import load as _load_prompt
from ..llm.provider import LLMProvider
from ..telemetry.tokens import TokenTracker

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = _load_prompt("mechanic")


class MechanicContext(str, Enum):
    MIGRATE = "migrate"
    REPAIR = "repair"


class MechanicAgent(BaseAgent):
    """Handles both MIGRATE (schema/config migration) and REPAIR (runtime error recovery)."""

    def __init__(
        self,
        context: str,
        mode: MechanicContext = MechanicContext.REPAIR,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
    ) -> None:
        super().__init__(
            task_id=-1,
            user_context=None,
            agent_class="mechanic",
            provider=provider,
            token_tracker=token_tracker,
        )
        self._context = context
        self._mode = mode

    def _system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def _build_initial_message(self) -> str:
        return f"[{self._mode.value.upper()} MODE]\n\n{self._context}"

    async def run_initial(self) -> str:
        """Start the mechanic session with the error/migration context."""
        logger.info("MechanicAgent[%s] starting %s", self.id, self._mode.value)
        result = await self.run(self._build_initial_message())
        logger.info("MechanicAgent[%s] initial pass complete", self.id)
        return result

    async def followup(self, message: str) -> str:
        """Continue multi-turn conversation with mechanic."""
        return await self.run(message)
