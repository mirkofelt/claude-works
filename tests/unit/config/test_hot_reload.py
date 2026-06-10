import claude_works.config as cfg


def test_set_replaces_config():
    cfg.set({"agents": {"models": {"generalist": "fast"}}})
    assert cfg.get_agent_model("generalist") == "claude-haiku-4-5-20251001"


def test_set_again_replaces():
    cfg.set({"agents": {"models": {"generalist": "best"}}})
    assert cfg.get_agent_model("generalist") == "claude-opus-4-8"


def test_config_updated_at_default():
    assert cfg._config_updated_at == 0 or isinstance(cfg._config_updated_at, int)


def test_set_updates_updated_at():
    cfg.set({"agents": {}})
    cfg._config_updated_at = 42
    assert cfg._config_updated_at == 42
