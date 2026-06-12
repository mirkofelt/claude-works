import claude_works.config as cfg
from claude_works.agents.specialist.generalist import GeneralistAgent


GROUPS = {
    "-1001234567890": {
        "persona": "Du bist der Hausmeister.",
        "focus": "Nur Hausautomation",
        "communication_style": "Knapp, Deutsch",
    }
}


def test_group_config_lookup_str_key(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {"groups": GROUPS})
    g = cfg.group_config(-1001234567890)
    assert g["focus"] == "Nur Hausautomation"
    assert g["communication_style"] == "Knapp, Deutsch"


def test_group_config_int_key(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {"groups": {-42: {"focus": "x"}}})
    assert cfg.group_config(-42)["focus"] == "x"


def test_group_config_direct_chat_returns_empty(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {"groups": GROUPS})
    assert cfg.group_config(12345) == {}
    assert cfg.group_config(None) == {}


def test_group_config_unconfigured_group(monkeypatch):
    monkeypatch.setattr(cfg, "_settings", {"groups": {}})
    assert cfg.group_config(-999) == {}


def test_focus_and_style_injected_into_prompt():
    agent = GeneralistAgent(
        task_id=0,
        user_context={
            "user_id": 1,
            "chat_id": -1001234567890,
            "is_group": True,
            "focus": "Nur Hausautomation",
            "communication_style": "Knapp, Deutsch",
        },
        persona="Du bist der Hausmeister.",
    )
    prompt = agent._system_prompt()
    assert "Nur Hausautomation" in prompt
    assert "Knapp, Deutsch" in prompt
    assert "Du bist der Hausmeister." in prompt
