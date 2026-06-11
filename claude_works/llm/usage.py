import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


# Month abbreviation → month number
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "Jun 11, 10pm" or "Jun 12, 6:59pm" → unix timestamp (UTC)
# Assumes current year, Europe/Berlin = UTC+2 (CEST, summer)
_RESET_RE = re.compile(
    r"resets\s+(\w+)\s+(\d+),\s+(\d+)(?::(\d+))?(am|pm)\s+\(([^)]+)\)",
    re.IGNORECASE,
)
_BERLIN_OFFSET = 2  # CEST (UTC+2); close enough for display purposes


def _parse_reset_unix(text: str) -> int | None:
    m = _RESET_RE.search(text)
    if not m:
        return None
    mon_str, day, hour_str, min_str, ampm, _tz = m.groups()
    mon = _MONTHS.get(mon_str.lower())
    if not mon:
        return None
    hour = int(hour_str)
    minute = int(min_str or 0)
    if ampm.lower() == "pm" and hour != 12:
        hour += 12
    elif ampm.lower() == "am" and hour == 12:
        hour = 0
    now = datetime.now(timezone.utc)
    year = now.year
    # Build naive datetime in Berlin local time, convert to UTC
    local_dt = datetime(year, mon, int(day), hour, minute)
    utc_ts = int(local_dt.timestamp()) - _BERLIN_OFFSET * 3600
    # If reset is in the past (>1h ago), assume next year
    if utc_ts < int(time.time()) - 3600:
        local_dt = datetime(year + 1, mon, int(day), hour, minute)
        utc_ts = int(local_dt.timestamp()) - _BERLIN_OFFSET * 3600
    return utc_ts


def _parse_pct(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if m:
        try:
            return float(m.group(1)) / 100.0
        except ValueError:
            pass
    return None


@dataclass
class UsageStats:
    """Parsed LLM CLI usage stats from /usage slash command."""

    # Legacy / token-count format
    tokens_used: int | None = None
    tokens_limit: int | None = None

    # Subscription percentage limits (Claude Max plan format)
    session_pct: float | None = None           # "Current session: X% used"
    weekly_all_pct: float | None = None        # "Current week (all models): X% used"
    weekly_models: list[tuple[str, float]] = field(default_factory=list)  # [(model_name, pct_0_to_1), ...]

    # Reset timestamps (unix)
    session_reset_at: int | None = None
    weekly_reset_at: int | None = None

    raw: str = field(default="", repr=False)

    @property
    def usage_pct(self) -> float | None:
        """Primary percentage — session or token-based."""
        if self.session_pct is not None:
            return self.session_pct
        if self.tokens_used is not None and self.tokens_limit:
            return self.tokens_used / self.tokens_limit
        return None

    @property
    def reset_in_seconds(self) -> int | None:
        if self.session_reset_at is not None:
            delta = self.session_reset_at - int(time.time())
            return max(0, delta)
        return None

    @property
    def is_near_limit(self) -> bool:
        pct = self.usage_pct
        return pct is not None and pct >= 0.8

    @property
    def is_critical(self) -> bool:
        pct = self.usage_pct
        return pct is not None and pct >= 0.95

    def as_dict(self) -> dict:
        now = int(time.time())
        return {
            "tokens_used": self.tokens_used,
            "tokens_limit": self.tokens_limit,
            "usage_pct": round(self.usage_pct * 100, 1) if self.usage_pct is not None else None,
            "session_pct": round(self.session_pct * 100, 1) if self.session_pct is not None else None,
            "weekly_all_pct": round(self.weekly_all_pct * 100, 1) if self.weekly_all_pct is not None else None,
            "weekly_models": [{"name": n, "pct": round(p * 100, 1)} for n, p in self.weekly_models],
            "session_reset_at": self.session_reset_at,
            "weekly_reset_at": self.weekly_reset_at,
            "reset_in_seconds": self.reset_in_seconds,
            "session_reset_in": max(0, self.session_reset_at - now) if self.session_reset_at else None,
            "weekly_reset_in": max(0, self.weekly_reset_at - now) if self.weekly_reset_at else None,
        }


# Line-by-line patterns for subscription format
_SESSION_RE = re.compile(r"current session[:\s]+", re.IGNORECASE)
_WEEKLY_ALL_RE = re.compile(r"current week\s*\(all models\)[:\s]+", re.IGNORECASE)
_WEEKLY_MODEL_RE = re.compile(r"current week\s*\(([^)]+?)\s*only\)[:\s]+", re.IGNORECASE)


def parse_usage_text(text: str) -> UsageStats:
    """Parse /usage output into UsageStats.

    Handles subscription format:
      Current session: 28% used · resets Jun 11, 10pm (Europe/Berlin)
      Current week (all models): 19% used · resets Jun 12, 7pm (Europe/Berlin)
      Current week (Sonnet only): 27% used · resets Jun 12, 6:59pm (Europe/Berlin)

    And legacy token-count format:
      1,234,567 / 5,000,000 (24.7%)
    """
    stats = UsageStats(raw=text)

    for line in text.splitlines():
        line = line.strip()
        if _SESSION_RE.match(line):
            rest = _SESSION_RE.sub("", line)
            stats.session_pct = _parse_pct(rest)
            stats.session_reset_at = _parse_reset_unix(rest)
        elif _WEEKLY_ALL_RE.match(line):
            rest = _WEEKLY_ALL_RE.sub("", line)
            stats.weekly_all_pct = _parse_pct(rest)
            stats.weekly_reset_at = _parse_reset_unix(rest)
        else:
            m2 = _WEEKLY_MODEL_RE.match(line)
            if m2:
                model_name = m2.group(1).strip()
                rest = _WEEKLY_MODEL_RE.sub("", line, count=1)
                pct = _parse_pct(rest)
                if pct is not None:
                    stats.weekly_models.append((model_name, pct))

    # Legacy: token counts "1,234,567 / 5,000,000"
    if stats.session_pct is None and stats.tokens_used is None:
        m = re.search(r"([\d,]+)\s*(?:/|of)\s*([\d,]+)", text)
        if m:
            try:
                stats.tokens_used = int(m.group(1).replace(",", ""))
                stats.tokens_limit = int(m.group(2).replace(",", ""))
            except ValueError:
                pass

    # Legacy: bare percentage "42.5%"
    if stats.usage_pct is None:
        m = re.search(r"(\d+\.?\d*)\s*%", text)
        if m:
            try:
                stats.session_pct = float(m.group(1)) / 100.0
            except ValueError:
                pass

    # Legacy: relative reset "Resets in 3h 42m", "Reset in 2h", "Resets in 1d 4h 30m"
    if stats.session_reset_at is None:
        m = re.search(
            r"reset[s]?\s+in\s+(?:(\d+)d\s*)?(?:(\d+)h\s*)?(?:(\d+)m)?",
            text, re.IGNORECASE,
        )
        if m and any(m.groups()):
            d = int(m.group(1) or 0)
            h = int(m.group(2) or 0)
            mm = int(m.group(3) or 0)
            stats.session_reset_at = int(time.time()) + d * 86400 + h * 3600 + mm * 60

    return stats
