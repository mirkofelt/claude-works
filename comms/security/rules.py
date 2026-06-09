import re
from dataclasses import dataclass


DEFAULT_RULES: list[dict] = [
    {"type": "internet_access", "pattern": r"https?://\S+", "enabled": True},
    {"type": "data_deletion", "pattern": r"\b(delete|drop|truncate|wipe|purge)\b", "enabled": True},
    {"type": "command_execution", "pattern": r"\b(execute|subprocess|shell|eval)\b", "enabled": True},
    {"type": "external_api", "pattern": r"\b(webhook|api_call|post_to)\b", "enabled": False},
    {"type": "publication", "pattern": r"\b(publish|broadcast|announcement)\b", "enabled": False},
]


@dataclass
class Rule:
    type: str
    pattern: str
    enabled: bool = True

    def __post_init__(self) -> None:
        self._compiled: re.Pattern | None = None

    def matches(self, text: str) -> bool:
        if not self.enabled:
            return False
        if self._compiled is None:
            self._compiled = re.compile(self.pattern, re.IGNORECASE | re.MULTILINE)
        return bool(self._compiled.search(text))


def build_rules(config_rules: list[dict] | None = None) -> list[Rule]:
    return [Rule(**r) for r in (config_rules if config_rules is not None else DEFAULT_RULES)]


def check_content(text: str, rules: list[Rule]) -> list[str]:
    """Return list of triggered action type names."""
    return [r.type for r in rules if r.matches(text)]
