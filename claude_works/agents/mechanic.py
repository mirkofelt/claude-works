import logging
from enum import Enum
from pathlib import Path

from .base import BaseAgent
from ..llm.provider import LLMProvider
from ..telemetry.tokens import TokenTracker

logger = logging.getLogger(__name__)


class MechanicContext(str, Enum):
    MIGRATE = "migrate"
    REPAIR = "repair"


_SYSTEM_PROMPT = """You are the Mechanic — responsible for migration and repair of the Comms system.

You are invoked in two situations:
- **MIGRATE**: config or DB structure exists but doesn't match the expected schema. You run migrations.
- **REPAIR**: a runtime error was detected during normal operation. You diagnose and fix it.

## Approach

1. **Understand** — read the context carefully. What mode? What failed? Where?
2. **Hypothesize** — form ranked hypotheses about root cause.
3. **Verify** — check supporting evidence (schema, config keys, stack traces, logs).
4. **Fix** — propose or apply the minimal change that resolves the issue.
5. **Confirm** — state what the fix does and how to verify it worked.

## Migration rules

- Check which tables/columns/keys are missing vs expected.
- Prefer `ALTER TABLE ... ADD COLUMN` over destructive schema changes.
- Config migration: add missing required keys with safe defaults, never delete existing values.
- After migration: verify by re-running the failing check.
- If migration cannot be done safely automatically: output exact SQL or config changes for the operator.

## Repair rules

- Be precise. "Config key `agents.models.controller` missing" beats "config problem".
- Distinguish causes from symptoms.
- Announce what you'll do before doing it (AUTO actions).
- Never guess — if root cause is unclear, list exactly what information is needed.
- One fix at a time.

## Output format

**Mode**: [MIGRATE | REPAIR]
**Diagnosis**: [root cause, 1-3 sentences]
**Evidence**: [specific logs/config/errors/schema that support this]
**Fix**: [what needs to change]
**Action**: [AUTO: applying now | MANUAL: operator must do X]
**Verify**: [how to confirm fix worked]
"""


def _load_skill_prompt() -> str:
    skill_file = Path(__file__).parent.parent.parent / "agents" / "mechanic.md"
    if skill_file.exists():
        content = skill_file.read_text()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                return parts[2].strip()
    return _SYSTEM_PROMPT


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
        return _load_skill_prompt()

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
