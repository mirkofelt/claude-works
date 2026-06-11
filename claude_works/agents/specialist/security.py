from ..base import BaseAgent
from ...llm.provider import LLMProvider
from ...telemetry.tokens import TokenTracker
from ...prompts import load as _load_prompt


class SecurityAgent(BaseAgent):
    def __init__(
        self,
        task_id: int,
        user_context: dict | None = None,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
        persona: str = "",
    ) -> None:
        super().__init__(task_id, user_context, "security", provider, token_tracker)

    def _system_prompt(self) -> str:
        return _load_prompt("security_health")
