import re as _re

from ..base import BaseAgent
from ..concepts import get_system_prompt, get_caveman_addendum
from ...llm.provider import LLMProvider
from ...telemetry.tokens import TokenTracker

_BOARD_TASK_SECTION_RE = _re.compile(
    r'\*\*Delegate to background board.*?(?=\*\*[A-Z]|\Z)',
    _re.DOTALL,
)


class GeneralistAgent(BaseAgent):
    def __init__(
        self,
        task_id: int,
        user_context: dict | None = None,
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
        persona: str = "",
        agent_class: str = "generalist",
        allow_subtasks: bool = True,
    ) -> None:
        super().__init__(task_id, user_context, agent_class, provider, token_tracker, allow_subtasks)
        self._persona = persona

    def _system_prompt(self) -> str:
        tools = get_system_prompt()
        if not self._allow_subtasks:
            tools = _BOARD_TASK_SECTION_RE.sub("", tools)
            tools = tools.replace("[BOARD_TASK: full task description with all context needed]\n", "")
        if self._persona:
            # Persona overrides character identity but tool/tag docs are always appended
            base = self._persona + "\n\n---\n\n" + tools
        else:
            base = tools
        if self._user_context.get("caveman_mode", True):
            base += get_caveman_addendum()
        return base + self._user_context_section()
