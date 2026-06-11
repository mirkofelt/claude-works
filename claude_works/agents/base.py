import logging
import re
import uuid
from abc import ABC, abstractmethod

from ..config import get_agent_model, section
from ..llm.provider import LLMProvider, get_provider
from ..telemetry import task_log as _tlog
from ..telemetry.tokens import BudgetExceededError, TokenTracker

logger = logging.getLogger(__name__)

CONTEXT_WARN_THRESHOLD = 0.5
CONTEXT_COMPACT_THRESHOLD = 0.6

_TOPIC_SHIFT_THRESHOLD = 0.10
_TOPIC_SHIFT_MIN_WORDS = 5
_TOPIC_SHIFT_MIN_MESSAGES = 4


class BaseAgent(ABC):
    def __init__(
        self,
        task_id: int,
        user_context: dict | None = None,
        agent_class: str = "generalist",
        provider: LLMProvider | None = None,
        token_tracker: TokenTracker | None = None,
    ) -> None:
        self.id = str(uuid.uuid4())[:8]
        self.task_id = task_id
        self.agent_class = agent_class
        self._user_context = user_context or {}
        self._provider = provider
        self._token_tracker = token_tracker
        self._messages: list[dict] = []
        self._context_tokens = 0
        self._owns_provider = provider is None

    @abstractmethod
    def _system_prompt(self) -> str:
        ...

    def _get_model(self) -> str:
        return get_agent_model(self.agent_class)

    def _get_provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = get_provider(section("llm"))
        return self._provider

    def _user_context_section(self) -> str:
        bg = self._user_context.get("background", "")
        if bg:
            return f"\n\n## User Context\nBackground: {bg}"
        return ""

    def _get_mcp_servers(self) -> list[dict] | None:
        cfg = section("mcp")
        if not cfg.get("enabled", False):
            return None
        servers = cfg.get("servers", [])
        return servers if servers else None

    async def run(self, content: str) -> str:
        cfg = section("llm")
        model = self._get_model()
        max_tokens = cfg.get("max_tokens", 8192)
        max_context = cfg.get("max_context_tokens", 150000)

        if self._token_tracker:
            user_id = self._user_context.get("user_id")
            model = await self._token_tracker.get_allowed_model(model, user_id=user_id)
            if model is None:
                raise BudgetExceededError("Spending limit reached — task rejected")

        if self._topic_shifted(content):
            logger.info("Agent %s[%s] topic shift — proactive compact", self.agent_class, self.id)
            await self._compact()

        self._messages.append({"role": "user", "content": content})

        logger.info(
            "Agent %s[%s] task=%d model=%s",
            self.agent_class, self.id, self.task_id, model,
        )
        _tlog.info(self.task_id, f"→ {self.agent_class} calling {model}")

        response = await self._get_provider().complete(
            self._messages,
            system=self._system_prompt(),
            model=model,
            max_tokens=max_tokens,
            mcp_servers=self._get_mcp_servers(),
        )

        self._messages.append({"role": "assistant", "content": response.text})
        self._context_tokens = response.usage.input_tokens + response.usage.output_tokens

        _tlog.info(
            self.task_id,
            f"← {self.agent_class} done  in={response.usage.input_tokens} out={response.usage.output_tokens} tokens",
        )

        if self._token_tracker:
            await self._token_tracker.log(
                agent_id=self.id,
                agent_class=self.agent_class,
                task_id=self.task_id,
                user_id=self._user_context.get("user_id"),
                chat_id=self._user_context.get("chat_id"),
                model=model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read_tokens=response.usage.cache_read_tokens,
                cache_write_tokens=response.usage.cache_write_tokens,
            )

        logger.info(
            "Agent %s[%s] done in=%d out=%d",
            self.agent_class, self.id,
            response.usage.input_tokens, response.usage.output_tokens,
        )

        utilization = self._context_tokens / max_context
        if utilization >= CONTEXT_COMPACT_THRESHOLD:
            logger.warning("Agent %s context %.0f%% — compacting", self.id, utilization * 100)
            await self._compact()
        elif utilization >= CONTEXT_WARN_THRESHOLD:
            logger.warning("Agent %s context %.0f%%", self.id, utilization * 100)

        return response.text

    def _topic_shifted(self, new_content: str) -> bool:
        if len(self._messages) < _TOPIC_SHIFT_MIN_MESSAGES:
            return False
        new_words = set(re.findall(r'\b\w{4,}\b', new_content.lower()))
        if len(new_words) < _TOPIC_SHIFT_MIN_WORDS:
            return False
        recent_text = " ".join(
            m["content"] for m in self._messages[-4:]
            if isinstance(m.get("content"), str)
        )
        recent_words = set(re.findall(r'\b\w{4,}\b', recent_text.lower()))
        if not recent_words:
            return False
        overlap = len(new_words & recent_words) / len(new_words | recent_words)
        return overlap < _TOPIC_SHIFT_THRESHOLD

    async def _compact(self) -> None:
        if len(self._messages) < 4:
            return
        history = "\n\n".join(
            f"{'USER' if m['role'] == 'user' else 'ASSISTANT'}: {m['content']}"
            for m in self._messages[:-2]
        )
        response = await self._get_provider().complete(
            [{"role": "user", "content": f"Summarize this conversation concisely:\n\n{history}"}],
            system="You are a concise summarizer.",
            model=get_agent_model("compactor"),
            max_tokens=1024,
        )
        self._messages = [
            {"role": "user", "content": f"[Context summary: {response.text}]"},
            {"role": "assistant", "content": "Understood."},
        ] + self._messages[-2:]
        logger.info("Agent %s compacted", self.id)

    @property
    def context_tokens(self) -> int:
        return self._context_tokens

    async def close(self) -> None:
        if self._owns_provider and self._provider:
            await self._provider.close()
