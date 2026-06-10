import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_BUILTIN_DIR = Path(__file__).parent
_DATA_DIR = Path(os.environ.get("PROMPTS_DIR", "/data/prompts"))


def load(name: str) -> str:
    """Load prompt by name. Checks /data/prompts/<name>.md first, falls back to built-in."""
    override = _DATA_DIR / f"{name}.md"
    if override.is_file():
        logger.debug("Prompt %r loaded from data dir override", name)
        return override.read_text(encoding="utf-8").strip()
    return (_BUILTIN_DIR / f"{name}.md").read_text(encoding="utf-8").strip()


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
