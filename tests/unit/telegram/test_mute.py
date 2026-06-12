"""Tests for hard-mute tag extraction and mute bookkeeping (main.py)."""
import time

from claude_works.main import _extract_mute_tag, _extract_unmute_tag


# ── [MUTE:] extraction ────────────────────────────────────────

def test_mute_tag_with_minutes():
    clean, v = _extract_mute_tag("Done. [MUTE: Paul | 60]")
    assert v == ("Paul", 60)
    assert "[MUTE" not in clean


def test_mute_tag_without_minutes_is_indefinite():
    _, v = _extract_mute_tag("[MUTE: 123456789]")
    assert v == ("123456789", 0)


def test_mute_tag_invalid_minutes_falls_back_to_indefinite():
    _, v = _extract_mute_tag("[MUTE: Paul | bald]")
    assert v == ("Paul", 0)


def test_mute_tag_absent():
    text = "Nothing to see here."
    clean, v = _extract_mute_tag(text)
    assert v is None
    assert clean == text


def test_mute_tag_strips_cleanly_midtext():
    clean, v = _extract_mute_tag("Before [MUTE: Paul | 5] After")
    assert v == ("Paul", 5)
    assert clean == "Before\nAfter"


# ── [UNMUTE:] extraction ──────────────────────────────────────

def test_unmute_tag():
    clean, ident = _extract_unmute_tag("Ok. [UNMUTE: Paul]")
    assert ident == "Paul"
    assert "[UNMUTE" not in clean


def test_unmute_tag_absent():
    clean, ident = _extract_unmute_tag("hello")
    assert ident is None
    assert clean == "hello"


# ── mute state logic (mirrors _is_muted semantics) ────────────

class _MuteState:
    """Minimal stand-in exercising the same expiry logic as Daemon._is_muted."""

    def __init__(self, muted):
        self._muted_users = muted

    def is_muted(self, tid):
        until = self._muted_users.get(tid)
        if until is None:
            return False
        if until != 0 and time.time() > until:
            del self._muted_users[tid]
            return False
        return True


def test_indefinite_mute_active():
    s = _MuteState({1: 0})
    assert s.is_muted(1)


def test_timed_mute_active():
    s = _MuteState({1: int(time.time()) + 600})
    assert s.is_muted(1)


def test_expired_mute_pruned():
    s = _MuteState({1: int(time.time()) - 10})
    assert not s.is_muted(1)
    assert 1 not in s._muted_users


def test_unknown_user_not_muted():
    assert not _MuteState({}).is_muted(42)
