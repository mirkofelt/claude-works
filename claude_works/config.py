import logging
from typing import Any


logger = logging.getLogger(__name__)

_settings: dict[str, Any] | None = None
_config_updated_at: int = 0  # DB updated_at timestamp; used for hot-reload detection

# Tier aliases: resolve via agents.model_tiers in settings.json first,
# fall back to these hardcoded IDs. Update here only when Anthropic
# retires an entire model generation.
_TIER_DEFAULTS: dict[str, str] = {
    "fast":     "claude-haiku-4-5-20251001",
    "balanced": "claude-sonnet-4-6",
    "best":     "claude-opus-4-8",
}

_TIER_ORDER = ["fast", "balanced", "best"]

# Per-agent class tier assignments (used when agents.models.<class> is absent).
_AGENT_CLASS_TIERS: dict[str, str] = {
    "default":    "balanced",
    "controller": "fast",
    "memory":     "fast",
    "compactor":  "fast",
    "mechanic":   "balanced",
    "chief":      "balanced",
    "po":         "balanced",
    "generalist": "balanced",
    "researcher": "balanced",
    "coder":      "balanced",
}

# Per-stage CodeTeam tier assignments (used when coder.<stage> is absent).
_CODER_STAGE_TIERS: dict[str, str] = {
    "architect": "balanced",
    "developer": "balanced",
    "tester":    "fast",
    "qa":        "balanced",
}

# Agent run timeouts in seconds. Override via config key "agent" in daemon_config
# (editable through Web UI /api/config — generic config save applies these too).
#   reply_timeout_seconds — hard cap for an inline chat run before background offload
#   idle_timeout_seconds  — abort when no agent activity for this long
#   max_runtime_seconds   — hard cap for background (board) task runs
_AGENT_TIMEOUT_DEFAULTS: dict[str, float] = {
    "reply_timeout_seconds": 300.0,
    "idle_timeout_seconds": 120.0,
    "max_runtime_seconds": 1800.0,
}

# Approximate pricing in USD per 1M tokens (verified 2026-06-12, platform.claude.com/docs).
# Cache rates: 5m cache write = 1.25x input, cache read = 0.1x input.
# Override per-model in settings.json spending.model_pricing when prices change.
_MODEL_PRICING_DEFAULTS: dict[str, dict] = {
    "claude-haiku-4-5-20251001": {
        "input_per_mtok": 1.00, "output_per_mtok": 5.00,
        "cache_read_per_mtok": 0.10, "cache_write_per_mtok": 1.25,
    },
    "claude-sonnet-4-6": {
        "input_per_mtok": 3.00, "output_per_mtok": 15.00,
        "cache_read_per_mtok": 0.30, "cache_write_per_mtok": 3.75,
    },
    "claude-opus-4-8": {
        "input_per_mtok": 5.00, "output_per_mtok": 25.00,
        "cache_read_per_mtok": 0.50, "cache_write_per_mtok": 6.25,
    },
    "claude-fable-5": {
        "input_per_mtok": 10.00, "output_per_mtok": 50.00,
        "cache_read_per_mtok": 1.00, "cache_write_per_mtok": 12.50,
    },
}


def set(cfg: dict[str, Any]) -> None:
    """Inject config dict (loaded from config.db). Called at startup and on hot-reload."""
    global _settings
    _settings = cfg


def get() -> dict[str, Any]:
    if _settings is None:
        raise RuntimeError("Config not initialised — call config.set() at startup")
    return _settings


def section(key: str) -> dict[str, Any]:
    return get().get(key, {})


def agent_timeout(key: str) -> float:
    """Return agent timeout in seconds from config section "agent", with default fallback.

    Invalid or non-positive values fall back to the hardcoded default so a broken
    config entry can never disable timeouts entirely.
    """
    if key not in _AGENT_TIMEOUT_DEFAULTS:
        raise KeyError(f"Unknown agent timeout key: {key!r}")
    default = _AGENT_TIMEOUT_DEFAULTS[key]
    raw = section("agent").get(key)
    if raw is None and key == "max_runtime_seconds":
        # Legacy key: agents.task_timeout_seconds was the wall-clock kill before
        # the heartbeat supervisor — existing configs keep working as hard cap.
        raw = section("agents").get("task_timeout_seconds")
    if raw is None:
        raw = default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _resolve_tier(value: str) -> str:
    """Resolve tier alias → model ID. Unknown strings returned unchanged (direct model ID)."""
    tiers = section("agents").get("model_tiers", {})
    if value in tiers:
        return tiers[value]
    if value in _TIER_DEFAULTS:
        return _TIER_DEFAULTS[value]
    return value


def get_agent_model(agent_class: str, stage: str | None = None) -> str:
    """Return model ID for agent_class (and optional CodeTeam stage).

    Lookup order:
      1. agents.models.<agent_class>[.<stage>]  — explicit override in config.db
      2. _AGENT_CLASS_TIERS / _CODER_STAGE_TIERS  — per-class tier assignment
      3. agents.model_tiers.<tier>  — tier → model ID mapping in config.db
      4. _TIER_DEFAULTS  — hardcoded tier fallbacks

    Values at any level may be tier aliases ("fast"/"balanced"/"best") or direct model IDs.
    To upgrade all "best"-tier agents: set agents.model_tiers.best in config.db.
    """
    models = section("agents").get("models", {})
    global_default = _resolve_tier(models.get("default", _AGENT_CLASS_TIERS["default"]))

    if agent_class == "coder" and stage:
        coder_cfg = models.get("coder", {})
        if isinstance(coder_cfg, str):
            return _resolve_tier(coder_cfg)
        stage_tier = _CODER_STAGE_TIERS.get(stage, "balanced")
        raw = coder_cfg.get(stage, coder_cfg.get("default", stage_tier))
        return _resolve_tier(raw)

    entry = models.get(agent_class)
    if entry is None:
        if agent_class in _AGENT_CLASS_TIERS:
            return _resolve_tier(_AGENT_CLASS_TIERS[agent_class])
        return global_default
    if isinstance(entry, str):
        return _resolve_tier(entry)
    return _resolve_tier(entry.get("default", _AGENT_CLASS_TIERS.get(agent_class, "balanced")))


def get_model_tier_name(model_id: str) -> str | None:
    """Return tier name ('fast'/'balanced'/'best') for a resolved model ID, or None."""
    tiers = section("agents").get("model_tiers", {})
    for name, mid in tiers.items():
        if mid == model_id:
            return name
    for name, mid in _TIER_DEFAULTS.items():
        if mid == model_id:
            return name
    return None


def downgrade_model(model_id: str) -> str | None:
    """Return next cheaper model ID, or None if already cheapest (fast tier)."""
    tier = get_model_tier_name(model_id)
    if tier not in _TIER_ORDER:
        return None
    idx = _TIER_ORDER.index(tier)
    if idx == 0:
        return None
    return _resolve_tier(_TIER_ORDER[idx - 1])


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimate cost in USD for a single API call.

    Includes prompt-cache tokens (API reports them separately from input_tokens;
    in agent workloads they dominate total cost). Unknown models are logged and
    return 0.0 — add them to spending.model_pricing or _MODEL_PRICING_DEFAULTS.
    """
    pricing = section("spending").get("model_pricing", {})
    rates = pricing.get(model) or _MODEL_PRICING_DEFAULTS.get(model)
    if not rates:
        logger.warning(
            "No pricing for model %s — cost booked as $0. Add to spending.model_pricing.",
            model,
        )
        return 0.0
    input_rate = rates.get("input_per_mtok", 0.0)
    return (
        input_tokens * input_rate
        + output_tokens * rates.get("output_per_mtok", 0.0)
        + cache_read_tokens * rates.get("cache_read_per_mtok", input_rate * 0.1)
        + cache_write_tokens * rates.get("cache_write_per_mtok", input_rate * 1.25)
    ) / 1_000_000
