import pytest
import claude_works.config as cfg


def _patch(monkeypatch, agent: dict | None = None):
    settings = {} if agent is None else {"agent": agent}
    monkeypatch.setattr(cfg, "_settings", settings)


def test_defaults_no_config(monkeypatch):
    _patch(monkeypatch)
    assert cfg.agent_timeout("reply_timeout_seconds") == 300.0
    assert cfg.agent_timeout("idle_timeout_seconds") == 120.0
    assert cfg.agent_timeout("max_runtime_seconds") == 1800.0


def test_override_from_config(monkeypatch):
    _patch(monkeypatch, {
        "reply_timeout_seconds": 600,
        "idle_timeout_seconds": 60,
        "max_runtime_seconds": 3600,
    })
    assert cfg.agent_timeout("reply_timeout_seconds") == 600.0
    assert cfg.agent_timeout("idle_timeout_seconds") == 60.0
    assert cfg.agent_timeout("max_runtime_seconds") == 3600.0


def test_partial_override_keeps_other_defaults(monkeypatch):
    _patch(monkeypatch, {"idle_timeout_seconds": 45})
    assert cfg.agent_timeout("idle_timeout_seconds") == 45.0
    assert cfg.agent_timeout("reply_timeout_seconds") == 300.0
    assert cfg.agent_timeout("max_runtime_seconds") == 1800.0


def test_string_value_is_coerced(monkeypatch):
    _patch(monkeypatch, {"reply_timeout_seconds": "450"})
    assert cfg.agent_timeout("reply_timeout_seconds") == 450.0


def test_invalid_value_falls_back_to_default(monkeypatch):
    _patch(monkeypatch, {"reply_timeout_seconds": "kaputt"})
    assert cfg.agent_timeout("reply_timeout_seconds") == 300.0


def test_non_positive_value_falls_back_to_default(monkeypatch):
    _patch(monkeypatch, {"idle_timeout_seconds": 0})
    assert cfg.agent_timeout("idle_timeout_seconds") == 120.0
    _patch(monkeypatch, {"idle_timeout_seconds": -5})
    assert cfg.agent_timeout("idle_timeout_seconds") == 120.0


def test_unknown_key_raises(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(KeyError):
        cfg.agent_timeout("nonexistent_timeout")


def test_max_runtime_falls_back_to_legacy_task_timeout(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {"agents": {"task_timeout_seconds": 900}})
    assert cfg.agent_timeout("max_runtime_seconds") == 900.0


def test_agent_section_wins_over_legacy_task_timeout(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {
        "agent": {"max_runtime_seconds": 1200},
        "agents": {"task_timeout_seconds": 900},
    })
    assert cfg.agent_timeout("max_runtime_seconds") == 1200.0
