from ..base import BaseAgent
from ..concepts import SYSTEM_PROMPT, CAVEMAN_ADDENDUM
from ...llm.provider import LLMProvider
from ...telemetry.tokens import TokenTracker

_CODER_ADDENDUM = """

## Coder Role
Focus: writing, reviewing, debugging code.
Standards: no credentials in code, English in code/comments, security by default.
Return working code. Minimal explanation unless asked.
"""


class CoderAgent(BaseAgent):
    def __init__(
        self,
        task_id: int,
        user_context: dict | None = None,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
        persona: str = "",
    ) -> None:
        super().__init__(task_id, user_context, "coder", provider, token_tracker)
        self._persona = persona

    def _system_prompt(self) -> str:
        base = self._persona or SYSTEM_PROMPT
        base += _CODER_ADDENDUM
        if self._user_context.get("caveman_mode", True):
            base += CAVEMAN_ADDENDUM
        return base
