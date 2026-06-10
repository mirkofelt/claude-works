from ..base import BaseAgent
from ..concepts import SYSTEM_PROMPT, CAVEMAN_ADDENDUM
from ...llm.provider import LLMProvider
from ...telemetry.tokens import TokenTracker

_RESEARCHER_ADDENDUM = """

## Researcher Role
Focus: information retrieval, synthesis, source evaluation.
Structure findings clearly. Call out uncertainty explicitly.
Prefer concrete facts over speculation.
"""


class ResearchAgent(BaseAgent):
    def __init__(
        self,
        task_id: int,
        user_context: dict | None = None,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
        persona: str = "",
    ) -> None:
        super().__init__(task_id, user_context, "researcher", provider, token_tracker)
        self._persona = persona

    def _system_prompt(self) -> str:
        base = self._persona or SYSTEM_PROMPT
        base += _RESEARCHER_ADDENDUM
        if self._user_context.get("caveman_mode", True):
            base += CAVEMAN_ADDENDUM
        return base + self._user_context_section()
