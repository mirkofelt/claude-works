import re
from dataclasses import dataclass, field


@dataclass
class UsageStats:
    """Parsed LLM CLI usage stats from /usage slash command."""

    tokens_used: int | None = None
    tokens_limit: int | None = None
    usage_pct: float | None = None      # 0.0 – 1.0
    reset_in_seconds: int | None = None
    raw: str = field(default="", repr=False)

    @property
    def is_near_limit(self) -> bool:
        return self.usage_pct is not None and self.usage_pct >= 0.8

    @property
    def is_critical(self) -> bool:
        return self.usage_pct is not None and self.usage_pct >= 0.95

    def as_dict(self) -> dict:
        return {
            "tokens_used": self.tokens_used,
            "tokens_limit": self.tokens_limit,
            "usage_pct": round(self.usage_pct * 100, 1) if self.usage_pct is not None else None,
            "reset_in_seconds": self.reset_in_seconds,
        }


def parse_usage_text(text: str) -> UsageStats:
    """Parse free-form /usage output into UsageStats.

    Handles formats like:
      "1,234,567 / 5,000,000 (24.7%)"
      "Tokens used: 1234567 of 5000000"
      "Reset in 3h 42m"
    """
    stats = UsageStats(raw=text)

    # Token counts: "1,234,567 / 5,000,000" or "1234567 of 5000000"
    m = re.search(r"([\d,]+)\s*(?:/|of)\s*([\d,]+)", text)
    if m:
        try:
            stats.tokens_used = int(m.group(1).replace(",", ""))
            stats.tokens_limit = int(m.group(2).replace(",", ""))
            if stats.tokens_limit > 0:
                stats.usage_pct = stats.tokens_used / stats.tokens_limit
        except ValueError:
            pass

    # Explicit percentage: "24.7%" (use as override if token parse failed)
    if stats.usage_pct is None:
        m = re.search(r"(\d+\.?\d*)\s*%", text)
        if m:
            try:
                stats.usage_pct = float(m.group(1)) / 100.0
            except ValueError:
                pass

    # Reset time: "in 3h 42m", "resets in 2h", "reset in 45m", "in 1d 2h"
    m = re.search(
        r"reset[s]?\s+in\s+(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m)?",
        text, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"\bin\s+(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m)?(?:\s*$|\s+until)",
            text, re.IGNORECASE,
        )
    if m and any(m.groups()):
        d = int(m.group(1) or 0)
        h = int(m.group(2) or 0)
        mm = int(m.group(3) or 0)
        stats.reset_in_seconds = d * 86400 + h * 3600 + mm * 60

    return stats
