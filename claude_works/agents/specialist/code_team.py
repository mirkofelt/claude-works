import asyncio
import logging
import uuid

from ..base import BaseAgent
from ..concepts import get_system_prompt, get_caveman_addendum, get_dev_standards
from ...config import get_agent_model
from ...llm.provider import LLMProvider
from ...telemetry.tokens import TokenTracker

logger = logging.getLogger(__name__)

_ARCHITECT_ADDENDUM = """

## Role: Architect
Produce a concise design document for the given task:
- Interfaces, modules, key types/functions
- Data flow and state management
- Constraints, edge cases, security considerations
- Explicit decisions with brief rationale
Output: design doc only. No code.
"""

_DEVELOPER_ADDENDUM = """
## Role: Developer
Implement based on the provided spec. Write complete, working code.
Return code only. Minimal prose.
"""

_TESTER_ADDENDUM = """
## Role: Tester
Write tests covering happy path, edge cases, error conditions.
No mocking of DB or external services — use in-memory / test doubles that exercise real logic.
Return test code only.
"""

_TESTER_SPEC_ADDENDUM = """
## Role: Tester
Write tests based on the spec and task description (implementation not yet available).
Cover happy path, edge cases, error conditions.
No mocking of DB or external services — use in-memory / test doubles.
Return test code only.
"""

_QA_ADDENDUM = """

## Role: QA
Review the implementation and tests against the spec.
- Flag any gaps, security issues, or spec violations
- Apply fixes inline
- Return the final, polished implementation + tests as a single cohesive response
"""


class _TeamMember(BaseAgent):
    """Generic CodeTeam member. Role-specific behavior via addendum."""

    def __init__(
        self,
        task_id: int,
        user_context: dict | None,
        provider: LLMProvider | None,
        token_tracker: TokenTracker | None,
        persona: str,
        addendum: str,
        stage: str,
    ) -> None:
        super().__init__(task_id, user_context, "coder", provider, token_tracker)
        self._persona = persona
        self._addendum = addendum
        self._stage = stage

    def _get_model(self) -> str:
        return get_agent_model("coder", stage=self._stage)

    def _system_prompt(self) -> str:
        base = self._persona or get_system_prompt()
        if self._stage in ("developer", "tester", "tester_spec"):
            base += get_dev_standards()
        base += self._addendum
        if self._user_context.get("caveman_mode", True):
            base += get_caveman_addendum()
        return base


class CodeTeam:
    """Runs Architect → Developer → Tester → QA pipeline for coding tasks.

    Externally matches BaseAgent constructor + run() signature so AgentCoordinator
    can use it as a drop-in replacement for CoderAgent.
    """

    def __init__(
        self,
        task_id: int,
        user_context: dict | None = None,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
        persona: str = "",
    ) -> None:
        self.id = str(uuid.uuid4())[:8]
        self._task_id = task_id
        self._user_context = user_context or {}
        self._provider = provider
        self._token_tracker = token_tracker
        self._persona = persona
        # Token attribution: every member call in this pipeline shares one run_id
        # (the CodeTeam id) so the 4 stages group into a single run. May be
        # overridden by the coordinator after construction.
        self.source = "coderteam"
        self.run_id = self.id

    def _member(self, addendum: str, stage: str) -> _TeamMember:
        member = _TeamMember(
            task_id=self._task_id,
            user_context=self._user_context,
            provider=self._provider,
            token_tracker=self._token_tracker,
            persona=self._persona,
            addendum=addendum,
            stage=stage,
        )
        member.source = self.source
        member.run_id = self.run_id
        return member

    _SMALL_TASK_THRESHOLD = 500

    async def run(self, content: str) -> str:
        logger.info("CodeTeam[%s] pipeline start task=%d", self.id, self._task_id)

        # Skip architect for small tasks (3.2)
        if len(content) < self._SMALL_TASK_THRESHOLD:
            spec = content
            logger.debug("CodeTeam[%s] small task — skipping architect", self.id)
        else:
            spec = await self._member(_ARCHITECT_ADDENDUM, "architect").run(content)
            logger.debug("CodeTeam[%s] arch done len=%d", self.id, len(spec))

        # Developer + Tester in parallel from spec (3.3)
        code, tests = await asyncio.gather(
            self._member(_DEVELOPER_ADDENDUM, "developer").run(
                f"## Task\n{content}\n\n## Architecture Spec\n{spec}"
            ),
            self._member(_TESTER_SPEC_ADDENDUM, "tester").run(
                f"## Task\n{content}\n\n## Architecture Spec\n{spec}"
            ),
        )
        logger.debug("CodeTeam[%s] dev+test done", self.id)

        final = await self._member(_QA_ADDENDUM, "qa").run(
            f"## Task\n{content}\n\n## Spec\n{spec}\n\n"
            f"## Implementation\n{code}\n\n## Tests\n{tests}"
        )
        logger.info("CodeTeam[%s] pipeline complete", self.id)

        return final
