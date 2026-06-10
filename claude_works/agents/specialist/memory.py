from ..base import BaseAgent
from ..concepts import get_system_prompt
from ...llm.provider import LLMProvider
from ...telemetry.tokens import TokenTracker

_MEMORY_ADDENDUM = """

## Memory Role
Focus: knowledge base management — store, retrieve, update, delete.
Be precise about what gets stored. Use structured tags for retrieval.
Confirm what was stored or retrieved.
"""


class MemoryAgent(BaseAgent):
    def __init__(
        self,
        task_id: int,
        user_context: dict | None = None,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
        persona: str = "",
    ) -> None:
        super().__init__(task_id, user_context, "memory", provider, token_tracker)
        self._persona = persona

    def _system_prompt(self) -> str:
        base = self._persona or get_system_prompt()
        return base + _MEMORY_ADDENDUM
