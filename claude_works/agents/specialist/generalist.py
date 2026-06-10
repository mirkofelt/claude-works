from ..base import BaseAgent
from ..concepts import get_system_prompt, get_caveman_addendum
from ...llm.provider import LLMProvider
from ...telemetry.tokens import TokenTracker


class GeneralistAgent(BaseAgent):
    def __init__(
        self,
        task_id: int,
        user_context: dict | None = None,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
        persona: str = "",
        agent_class: str = "generalist",
    ) -> None:
        super().__init__(task_id, user_context, agent_class, provider, token_tracker)
        self._persona = persona

    def _system_prompt(self) -> str:
        base = self._persona or get_system_prompt()
        if self._user_context.get("caveman_mode", True):
            base += get_caveman_addendum()
        return base + self._user_context_section()
