import time
from .models import IncomingMessage


BUNDLE_TIME_WINDOW = 5.0  # seconds


def should_bundle(pending: IncomingMessage, incoming: IncomingMessage) -> bool:
    """Return True if incoming should be bundled with pending rather than a new task."""
    if pending.from_user_id != incoming.from_user_id:
        return False
    if pending.chat_id != incoming.chat_id:
        return False

    time_delta = incoming.timestamp - pending.timestamp
    if time_delta > BUNDLE_TIME_WINDOW:
        return False

    # Context-based bundling: treat as same task if previous message looks incomplete
    if pending.text and _is_open_ended(pending.text):
        return True

    # Short follow-up (< 20 chars) likely a continuation
    if incoming.text and len(incoming.text.strip()) < 20:
        return True

    return False


def _is_open_ended(text: str) -> bool:
    stripped = text.strip()
    return (
        stripped.endswith("...")
        or stripped.endswith(",")
        or stripped.endswith(":")
        or stripped.endswith("-")
    )


def merge_content(first: str | None, second: str | None) -> str:
    parts = [p for p in [first, second] if p]
    return "\n".join(parts)
