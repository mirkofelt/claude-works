import json
import os
from pathlib import Path
from typing import Any


_settings: dict[str, Any] | None = None
_settings_path: Path | None = None
_settings_mtime: float = 0.0

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

# Approximate pricing in USD per 1M tokens.
# Override per-model in settings.json spending.model_pricing when prices change.
_MODEL_PRICING_DEFAULTS: dict[str, dict] = {
    "claude-haiku-4-5-20251001": {"input_per_mtok": 0.80,  "output_per_mtok": 4.00},
    "claude-sonnet-4-6":         {"input_per_mtok": 3.00,  "output_per_mtok": 15.00},
    "claude-opus-4-8":           {"input_per_mtok": 15.00, "output_per_mtok": 75.00},
}


def _find_settings_file() -> Path:
    candidates = [
        os.environ.get("SETTINGS_FILE"),
        "/data/settings.json",
        Path(__file__).parent.parent / "settings.json",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c)
    raise FileNotFoundError("settings.json not found. Set SETTINGS_FILE env var.")


def load(path: Path | None = None) -> dict[str, Any]:
    global _settings, _settings_path, _settings_mtime
    _settings_path = path or _find_settings_file()
    _settings = json.loads(_settings_path.read_text())
    _settings_mtime = _settings_path.stat().st_mtime
    return _settings


def get() -> dict[str, Any]:
    if _settings is None:
        load()
    return _settings  # type: ignore[return-value]


def reload() -> dict[str, Any]:
    global _settings, _settings_mtime
    if _settings_path is None:
        return load()
    _settings = json.loads(_settings_path.read_text())
    _settings_mtime = _settings_path.stat().st_mtime
    return _settings


def reload_if_changed() -> bool:
    """Reload settings if file has been modified since last load. Returns True if reloaded."""
    if _settings_path is None:
        return False
    try:
        mtime = _settings_path.stat().st_mtime
    except OSError:
        return False
    if mtime <= _settings_mtime:
        return False
    reload()
    return True


def section(key: str) -> dict[str, Any]:
    return get().get(key, {})


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
      1. agents.models.<agent_class>[.<stage>]  — explicit override in settings.json
      2. _AGENT_CLASS_TIERS / _CODER_STAGE_TIERS  — per-class tier assignment
      3. agents.model_tiers.<tier>  — tier → model ID mapping in settings.json
      4. _TIER_DEFAULTS  — hardcoded tier fallbacks

    Values at any level may be tier aliases ("fast"/"balanced"/"best") or direct model IDs.
    To upgrade all "best"-tier agents: set agents.model_tiers.best in settings.json.
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


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a single API call. Returns 0.0 for unknown models."""
    pricing = section("spending").get("model_pricing", {})
    rates = pricing.get(model) or _MODEL_PRICING_DEFAULTS.get(model)
    if not rates:
        return 0.0
    return (
        input_tokens * rates.get("input_per_mtok", 0.0)
        + output_tokens * rates.get("output_per_mtok", 0.0)
    ) / 1_000_000
