import logging
import logging.handlers
import os
from pathlib import Path


_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

# Loggers that are noisy with low-value messages — suppress to WARNING
_SUPPRESS_LOGGERS = (
    "httpx", "httpcore", "hpack", "h2",
    "aiosqlite", "sqlite3",
    "uvicorn.access",        # HTTP access log — every GET /api/status etc.
    "watchfiles",
)


def setup(log_dir: str | None = None, log_level: str | None = None) -> None:
    from . import config as _config
    try:
        cfg = _config.section("logging")
    except Exception:
        cfg = {}

    level_name = log_level or os.environ.get("LOG_LEVEL") or cfg.get("level", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)
    resolved_log_dir = log_dir or cfg.get("dir", "/data/logs")

    fmt = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)

    Path(resolved_log_dir).mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        Path(resolved_log_dir) / "claude_works.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)  # file matches configured level, not always DEBUG

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)

    for noisy in _SUPPRESS_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging configured: level=%s dir=%s", level_name, resolved_log_dir
    )


def uvicorn_log_config() -> dict:
    """Uvicorn log_config that propagates into our root logger."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": _LOG_FORMAT, "datefmt": _DATE_FORMAT},
        },
        "handlers": {},
        "loggers": {
            "uvicorn": {"propagate": True, "level": "WARNING"},
            "uvicorn.error": {"propagate": True, "level": "WARNING"},
            "uvicorn.access": {"propagate": False, "level": "WARNING"},
        },
    }


def log_path(log_dir: str = "/data/logs") -> Path:
    return Path(log_dir) / "claude_works.log"
