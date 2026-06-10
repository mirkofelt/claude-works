import pytest
import claude_works.config as cfg


def _patch(monkeypatch, agents: dict):
    monkeypatch.setattr(cfg, "_settings", {"agents": agents})


def test_defaults_no_config(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    assert cfg.get_agent_model("controller") == "claude-haiku-4-5-20251001"
    assert cfg.get_agent_model("memory") == "claude-haiku-4-5-20251001"
    assert cfg.get_agent_model("generalist") == "claude-sonnet-4-6"
    assert cfg.get_agent_model("compactor") == "claude-haiku-4-5-20251001"
    assert cfg.get_agent_model("chief") == "claude-sonnet-4-6"


def test_tier_resolution(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    assert cfg._resolve_tier("fast") == "claude-haiku-4-5-20251001"
    assert cfg._resolve_tier("balanced") == "claude-sonnet-4-6"
    assert cfg._resolve_tier("best") == "claude-opus-4-8"
    assert cfg._resolve_tier("claude-opus-4-8") == "claude-opus-4-8"


def test_custom_tier_in_settings(monkeypatch):
    _patch(monkeypatch, {
        "model_tiers": {"best": "claude-fable-5"},
        "models": {"chief": "best"},
    })
    assert cfg.get_agent_model("chief") == "claude-fable-5"


def test_tier_alias_in_models(monkeypatch):
    _patch(monkeypatch, {"models": {"generalist": "best"}})
    assert cfg.get_agent_model("generalist") == "claude-opus-4-8"


def test_direct_model_id_override(monkeypatch):
    _patch(monkeypatch, {"models": {"generalist": "claude-opus-4-8"}})
    assert cfg.get_agent_model("generalist") == "claude-opus-4-8"
    assert cfg.get_agent_model("researcher") == "claude-sonnet-4-6"


def test_global_default_tier(monkeypatch):
    _patch(monkeypatch, {"models": {"default": "best"}})
    # unknown class uses global default
    assert cfg.get_agent_model("unknown_class") == "claude-opus-4-8"
    # per-class tier overrides global default
    assert cfg.get_agent_model("controller") == "claude-haiku-4-5-20251001"


def test_coder_stage_defaults(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {})
    assert cfg.get_agent_model("coder", stage="architect") == "claude-sonnet-4-6"
    assert cfg.get_agent_model("coder", stage="tester") == "claude-haiku-4-5-20251001"
    assert cfg.get_agent_model("coder", stage="developer") == "claude-sonnet-4-6"
    assert cfg.get_agent_model("coder", stage="qa") == "claude-sonnet-4-6"


def test_coder_stage_tier_override(monkeypatch):
    _patch(monkeypatch, {
        "models": {"coder": {"default": "balanced", "tester": "best"}}
    })
    assert cfg.get_agent_model("coder", stage="tester") == "claude-opus-4-8"
    assert cfg.get_agent_model("coder", stage="architect") == "claude-sonnet-4-6"


def test_coder_stage_custom_tier(monkeypatch):
    _patch(monkeypatch, {
        "model_tiers": {"best": "claude-fable-5"},
        "models": {"coder": {"qa": "best"}},
    })
    assert cfg.get_agent_model("coder", stage="qa") == "claude-fable-5"
    assert cfg.get_agent_model("coder", stage="tester") == "claude-haiku-4-5-20251001"


def test_coder_string_config_ignores_stage(monkeypatch):
    _patch(monkeypatch, {"models": {"coder": "best"}})
    assert cfg.get_agent_model("coder", stage="architect") == "claude-opus-4-8"
    assert cfg.get_agent_model("coder", stage="tester") == "claude-opus-4-8"
