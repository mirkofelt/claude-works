import json
import time
import pytest
import claude_works.config as cfg


def test_reload_if_changed_no_change(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"agents": {"models": {"default": "balanced"}}}))
    cfg.load(settings_file)
    assert cfg.reload_if_changed() is False


def test_reload_if_changed_detects_change(tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"agents": {"models": {"generalist": "fast"}}}))
    cfg.load(settings_file)
    assert cfg.get_agent_model("generalist") == "claude-haiku-4-5-20251001"

    import os
    new_mtime = settings_file.stat().st_mtime + 1
    settings_file.write_text(json.dumps({"agents": {"models": {"generalist": "best"}}}))
    os.utime(settings_file, (new_mtime, new_mtime))

    assert cfg.reload_if_changed() is True
    assert cfg.get_agent_model("generalist") == "claude-opus-4-8"


def test_reload_if_changed_no_path(monkeypatch):
    monkeypatch.setattr(cfg, "_settings_path", None)
    monkeypatch.setattr(cfg, "_settings_mtime", 0.0)
    assert cfg.reload_if_changed() is False
