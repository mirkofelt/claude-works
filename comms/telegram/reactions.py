from typing import Protocol


DEFAULT_REACTION_MAP = {
    "👍": "approve",
    "👎": "reject",
    "❤️": "save",
    "🔥": "prioritize",
    "😂": "dismiss",
    "🤔": "clarify",
}


def resolve_action(emoji: str, custom_map: dict[str, str] | None = None) -> str | None:
    mapping = {**DEFAULT_REACTION_MAP, **(custom_map or {})}
    return mapping.get(emoji)


def extract_reaction_emoji(new_reaction: list[dict]) -> str | None:
    if not new_reaction:
        return None
    first = new_reaction[0]
    if first.get("type") == "emoji":
        return first.get("emoji")
    return None
