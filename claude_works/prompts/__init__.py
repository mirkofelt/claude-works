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

# cache: name -> (mtime_key, checked_at_monotonic, text)
# mtime_key is a tuple of mtimes that contributed to the text
_cache: dict[str, tuple[tuple, float, str]] = {}


def load(name: str) -> str:
    """Load prompt by name.

    Resolution order (first match wins for base):
    1. /data/prompts/{name}.override.md  — full user override (replaces builtin)
    2. built-in {name}.md                — always up-to-date from image

    After resolving the base, /data/prompts/{name}.user.md is appended if present.
    Both user files are hot-reloaded within PROMPTS_STAT_TTL seconds (default 5s).

    Legacy /data/prompts/{name}.md files are no longer used as overrides — they were
    system copies and have been migrated away by migrate_legacy().
    """
    now = time.monotonic()
    cached = _cache.get(name)

    override = _DATA_DIR / f"{name}.override.md"
    builtin = _BUILTIN_DIR / f"{name}.md"
    user_ext = _DATA_DIR / f"{name}.user.md"

    base_path = override if override.is_file() else builtin

    try:
        base_mtime = base_path.stat().st_mtime
    except OSError:
        raise FileNotFoundError(f"Prompt not found: {name!r} (checked {override}, {builtin})")

    ext_mtime = user_ext.stat().st_mtime if user_ext.is_file() else 0.0
    mtime_key = (base_mtime, ext_mtime)

    if cached:
        old_key, checked_at, text = cached
        if now - checked_at < _STAT_TTL:
            return text
        if old_key == mtime_key:
            _cache[name] = (mtime_key, now, text)
            return text

    base_text = base_path.read_text(encoding="utf-8").strip()
    if ext_mtime:
        ext_text = user_ext.read_text(encoding="utf-8").strip()
        text = base_text + "\n\n" + ext_text if ext_text else base_text
    else:
        text = base_text

    if cached and cached[0] != mtime_key:
        logger.info("Prompt %r reloaded (file changed)", name)
    _cache[name] = (mtime_key, now, text)
    return text


def migrate_legacy() -> int:
    """Remove stale system-prompt copies from /data/prompts/<name>.md.

    Previously export_defaults() copied built-in prompts there. They're now
    loaded directly from the image. Files identical to the current built-in are
    deleted; files the user modified are renamed to <name>.override.md.
    Returns number of files removed or renamed.
    """
    if not _DATA_DIR.is_dir():
        return 0
    count = 0
    system_names = {p.stem for p in _BUILTIN_DIR.glob("*.md")}
    for legacy in _DATA_DIR.glob("*.md"):
        if legacy.stem not in system_names:
            continue  # not a system prompt name — leave alone
        if legacy.name.endswith((".user.md", ".override.md")):
            continue  # already in new format
        builtin = _BUILTIN_DIR / legacy.name
        if not builtin.is_file():
            continue
        legacy_text = legacy.read_text(encoding="utf-8")
        builtin_text = builtin.read_text(encoding="utf-8")
        if legacy_text.strip() == builtin_text.strip():
            legacy.unlink()
            logger.info("Prompts: removed stale system copy %s", legacy.name)
        else:
            override = _DATA_DIR / f"{legacy.stem}.override.md"
            legacy.rename(override)
            logger.info("Prompts: renamed customized %s → %s", legacy.name, override.name)
        count += 1
    return count


def export_defaults() -> int:
    """Create /data/prompts/ dir and run legacy migration. No longer copies system prompts."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("Cannot create prompts data dir %s: %s", _DATA_DIR, e)
        return 0
    return migrate_legacy()
