import logging
import os
from enum import Enum

logger = logging.getLogger(__name__)


class DaemonMode(str, Enum):
    STARTUP = "startup"
    INITIALIZE = "initialize"
    MIGRATE = "migrate"
    RUN = "run"
    REPAIR = "repair"


class ModeManager:
    def __init__(self) -> None:
        self._mode = DaemonMode.STARTUP
        self._error: str | None = None

    @property
    def mode(self) -> DaemonMode:
        return self._mode

    @property
    def error(self) -> str | None:
        return self._error

    def transition(self, mode: DaemonMode, error: str | None = None) -> None:
        prev = self._mode
        self._mode = mode
        self._error = error
        logger.info("Mode: %s → %s", prev.value, mode.value)

    def enter_repair(self, error: str) -> None:
        self.transition(DaemonMode.REPAIR, error)

    def exit_repair(self) -> None:
        self.transition(DaemonMode.RUN)

    def as_dict(self) -> dict:
        result: dict = {"mode": self._mode.value}
        if self._error:
            result["error"] = self._error
        return result


def _config_valid(cfg: dict) -> str | None:
    """Return error string if cfg is invalid, else None."""
    tg_token = cfg.get("telegram", {}).get("token", "")
    if not tg_token or tg_token == "YOUR_BOT_TOKEN":
        return "telegram.token not configured"
    auth_token = cfg.get("web", {}).get("auth_token", "")
    if not auth_token or auth_token == "YOUR_AUTH_TOKEN_HERE":
        return "web.auth_token not configured"
    return None


async def detect_startup_mode() -> tuple[DaemonMode, str | None]:
    """Probe config + DB. Returns (mode, optional_reason).

    Side effect: loads config if successful (so the rest of startup can use it).
    Tries DB-stored config first, falls back to settings.json file.
    """
    from . import config, db
    from .config_store import load_config as load_db_config

    data_db_path = os.environ.get("DB_FILE", "/data/claude-works.db")
    data_db_exists = os.path.exists(data_db_path)

    # Try config DB first (config.db is separate from data claude-works.db)
    try:
        conn = await db.init_config()
        db_cfg = await load_db_config(conn)
        await conn.close()
        if db_cfg:
            err = _config_valid(db_cfg)
            if err:
                return DaemonMode.INITIALIZE, f"DB config invalid: {err}"
            config._settings = db_cfg
            return DaemonMode.RUN, None
    except Exception as exc:
        logger.debug("Config DB init failed: %s", exc)
        # Not MIGRATE — config DB failure just means fall through to file check

    # Fall back to settings.json file
    try:
        config.load()
    except FileNotFoundError as exc:
        return DaemonMode.INITIALIZE, f"settings.json not found: {exc}"
    except Exception as exc:
        return DaemonMode.INITIALIZE, f"settings.json invalid: {exc}"

    err = _config_valid(config.get())
    if err:
        return DaemonMode.INITIALIZE, err

    # Check data DB for schema mismatch
    try:
        conn = await db.init()
        await conn.close()
    except Exception as exc:
        if data_db_exists:
            return DaemonMode.MIGRATE, f"DB schema mismatch: {exc}"
        return DaemonMode.INITIALIZE, f"DB init failed on fresh install: {exc}"

    return DaemonMode.RUN, None
