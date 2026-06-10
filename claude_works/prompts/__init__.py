import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_BUILTIN_DIR = Path(__file__).parent
_DATA_DIR = Path(os.environ.get("PROMPTS_DIR", "/data/prompts"))

# TTL before re-checking mtime. Within this window, load() returns cached
# text with zero disk I/O. Change becomes visible within STAT_TTL seconds.
_STAT_TTL: float = float(os.environ.get("PROMPTS_STAT_TTL", "5"))

# cache: name -> (mtime, checked_at_monotonic, text)
_cache: dict[str, tuple[float, float, str]] = {}


def load(name: str) -> str:
    """Load prompt by name. Checks /data/prompts/<name>.md first, falls back to built-in.
    Cached in memory; mtime checked at most every PROMPTS_STAT_TTL seconds (default 5s)."""
    now = time.monotonic()
    cached = _cache.get(name)

    if cached:
        mtime, checked_at, text = cached
        if now - checked_at < _STAT_TTL:
            return text  # within TTL — no disk I/O at all

    # TTL expired (or cold start) — check mtime
    override = _DATA_DIR / f"{name}.md"
    builtin = _BUILTIN_DIR / f"{name}.md"
    path = override if override.is_file() else builtin

    try:
        new_mtime = path.stat().st_mtime
    except OSError:
        raise FileNotFoundError(f"Prompt not found: {name!r} (checked {override}, {builtin})")

    if cached and cached[0] == new_mtime:
        # mtime unchanged — refresh TTL timestamp, return cached text
        _cache[name] = (new_mtime, now, cached[2])
        return cached[2]

    # file changed (or cold start) — read from disk
    text = path.read_text(encoding="utf-8").strip()
    _cache[name] = (new_mtime, now, text)
    if cached:
        logger.info("Prompt %r reloaded (file changed)", name)
    return text


def export_defaults() -> int:
    """Copy built-in prompts to data dir if not already present. Returns count of files written."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create prompts data dir %s: %s", _DATA_DIR, e)
        return 0
    written = 0
    for src in _BUILTIN_DIR.glob("*.md"):
        dest = _DATA_DIR / src.name
        if not dest.exists():
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            written += 1
    if written:
        logger.info("Exported %d default prompt(s) to %s", written, _DATA_DIR)
    return written
