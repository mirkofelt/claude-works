import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .errors import RateLimitError
from .usage import UsageStats, parse_usage_text


@dataclass
class LLMUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class LLMResponse:
    text: str
    usage: LLMUsage
    stop_reason: str = "end_turn"


class LLMProvider(ABC):
    """Abstract LLM provider — implement to add new backends."""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        *,
        system: str,
        model: str,
        max_tokens: int,
        mcp_servers: list[dict] | None = None,
    ) -> LLMResponse:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

    async def query_usage(self) -> UsageStats | None:
        return None


class APIProvider(LLMProvider):
    def __init__(self, api_key: str) -> None:
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        messages: list[dict],
        *,
        system: str,
        model: str,
        max_tokens: int,
        mcp_servers: list[dict] | None = None,
    ) -> LLMResponse:
        import anthropic as _anthropic

        try:
            if mcp_servers:
                response = await self._client.beta.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    mcp_servers=mcp_servers,
                    betas=["mcp-client-2025-04-04"],
                )
                text = "\n".join(
                    block.text for block in response.content if hasattr(block, "text")
                )
            else:
                response = await self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                )
                text = response.content[0].text
        except _anthropic.RateLimitError as exc:
            retry_after: float | None = None
            resp = getattr(exc, "response", None)
            if resp is not None:
                ra = getattr(resp, "headers", {}).get("retry-after")
                if ra:
                    try:
                        retry_after = float(ra)
                    except ValueError:
                        pass
            raise RateLimitError(str(exc), retry_after=retry_after) from exc

        usage = response.usage
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0),
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0),
            ),
            stop_reason=response.stop_reason or "end_turn",
        )

    async def close(self) -> None:
        await self._client.close()


class CliProvider(LLMProvider):
    """LLM provider via CLI tool (e.g. --print mode).

    Requires the CLI binary in PATH. No API key needed — uses the subscription
    associated with the CLI installation.
    Multi-turn history is embedded in the system prompt preamble.
    """

    def __init__(self, cli_binary: str) -> None:
        self._binary = cli_binary

    async def complete(
        self,
        messages: list[dict],
        *,
        system: str,
        model: str,
        max_tokens: int,
        mcp_servers: list[dict] | None = None,
    ) -> LLMResponse:
        full_system = system
        if len(messages) > 1:
            history = "\n\n".join(
                f"[{m['role'].upper()}]: {m['content']}" for m in messages[:-1]
            )
            full_system = f"{system}\n\n---\nConversation history:\n{history}\n---"

        user_msg = messages[-1]["content"] if messages else ""

        cmd = [
            self._binary,
            "--print",
            "--output-format", "json",
            "--model", model,
            "--system-prompt", full_system,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate(input=user_msg.encode())

        if proc.returncode != 0:
            stderr_text = stderr.decode()
            if re.search(r"rate.?limit|429|too many requests", stderr_text, re.IGNORECASE):
                retry_after = None
                m = re.search(r"retry.after[:\s]+(\d+)", stderr_text, re.IGNORECASE)
                if m:
                    retry_after = float(m.group(1))
                raise RateLimitError(
                    f"LLM CLI rate limited: {stderr_text[:200]}", retry_after=retry_after
                )
            raise RuntimeError(f"LLM CLI exited {proc.returncode}: {stderr.decode()[:500]}")

        data = json.loads(stdout.decode())
        if data.get("type") == "error" or data.get("subtype") == "error":
            raise RuntimeError(f"LLM CLI error: {data.get('error', data)}")

        text = data.get("result", "")
        usage = data.get("usage", {})
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
            ),
            stop_reason=data.get("stop_reason", "end_turn"),
        )

    async def query_usage(self) -> UsageStats | None:
        """Query current usage via CLI /usage slash command."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary, "--print", "--output-format", "json",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(input=b"/usage"), timeout=15.0)
        except Exception:
            return None

        if proc.returncode != 0:
            return None

        try:
            data = json.loads(stdout.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

        text = data.get("result", "")
        if not text:
            return None

        stats = parse_usage_text(text)
        return stats if (stats.tokens_used is not None or stats.usage_pct is not None) else None

    async def close(self) -> None:
        pass


def get_provider(cfg: dict) -> LLMProvider:
    """Factory: instantiate LLM provider from config dict."""
    provider_type = cfg.get("provider", "api")
    if provider_type == "api":
        return APIProvider(api_key=cfg["api_key"])
    if provider_type == "cli":
        binary = cfg.get("cli_binary", "")
        if not binary:
            raise ValueError("llm.cli_binary is required when provider is 'cli'")
        if not re.match(r'^[a-zA-Z0-9_./-]+$', binary):
            raise ValueError(f"llm.cli_binary contains invalid characters: {binary!r}")
        return CliProvider(cli_binary=binary)
    raise ValueError(f"Unknown LLM provider: {provider_type!r}")
