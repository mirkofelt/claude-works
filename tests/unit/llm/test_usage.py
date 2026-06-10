import pytest
from claude_works.llm.usage import UsageStats, parse_usage_text


def test_parse_token_slash_format():
    text = "Tokens: 1,234,567 / 5,000,000 (24.7%)"
    s = parse_usage_text(text)
    assert s.tokens_used == 1_234_567
    assert s.tokens_limit == 5_000_000
    assert abs(s.usage_pct - 1_234_567 / 5_000_000) < 0.001


def test_parse_token_of_format():
    text = "Usage: 800000 of 1000000 tokens used"
    s = parse_usage_text(text)
    assert s.tokens_used == 800_000
    assert s.tokens_limit == 1_000_000
    assert abs(s.usage_pct - 0.8) < 0.001


def test_parse_percentage_only():
    text = "Current usage: 42.5% of limit"
    s = parse_usage_text(text)
    assert s.usage_pct is not None
    assert abs(s.usage_pct - 0.425) < 0.001
    assert s.tokens_used is None


def test_parse_reset_hours_minutes():
    text = "Resets in 3h 42m"
    s = parse_usage_text(text)
    assert s.reset_in_seconds == 3 * 3600 + 42 * 60


def test_parse_reset_hours_only():
    text = "Reset in 2h"
    s = parse_usage_text(text)
    assert s.reset_in_seconds == 2 * 3600


def test_parse_reset_days():
    text = "Resets in 1d 4h 30m"
    s = parse_usage_text(text)
    assert s.reset_in_seconds == 86400 + 4 * 3600 + 30 * 60


def test_parse_empty_returns_none_fields():
    s = parse_usage_text("no data here")
    assert s.tokens_used is None
    assert s.tokens_limit is None
    assert s.usage_pct is None
    assert s.reset_in_seconds is None


def test_is_near_limit_true():
    s = UsageStats(usage_pct=0.85)
    assert s.is_near_limit


def test_is_near_limit_false():
    s = UsageStats(usage_pct=0.5)
    assert not s.is_near_limit


def test_is_critical():
    s = UsageStats(usage_pct=0.97)
    assert s.is_critical
    assert s.is_near_limit


def test_as_dict_rounds_pct():
    s = UsageStats(tokens_used=250000, tokens_limit=1000000, usage_pct=0.25, reset_in_seconds=3600)
    d = s.as_dict()
    assert d["usage_pct"] == 25.0
    assert d["tokens_used"] == 250000
    assert d["reset_in_seconds"] == 3600
