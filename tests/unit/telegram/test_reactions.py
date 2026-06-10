from claude_works.telegram.reactions import resolve_action, extract_reaction_emoji


def test_resolve_default_thumbsup():
    assert resolve_action("👍") == "approve"


def test_resolve_default_thumbsdown():
    assert resolve_action("👎") == "reject"


def test_resolve_custom_overrides_default():
    custom = {"👍": "custom_action"}
    assert resolve_action("👍", custom) == "custom_action"


def test_resolve_unknown_emoji():
    assert resolve_action("🦊") is None


def test_extract_emoji():
    reaction = [{"type": "emoji", "emoji": "👍"}]
    assert extract_reaction_emoji(reaction) == "👍"


def test_extract_empty():
    assert extract_reaction_emoji([]) is None


def test_extract_non_emoji_type():
    reaction = [{"type": "custom_emoji", "custom_emoji_id": "abc"}]
    assert extract_reaction_emoji(reaction) is None
