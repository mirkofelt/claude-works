import time
import pytest
from claude_works.tasks.bundler import should_bundle, merge_content, _is_open_ended
from claude_works.tasks.models import IncomingMessage


def _msg(text: str, ts: int, user_id: int = 1, chat_id: int = 100) -> IncomingMessage:
    return IncomingMessage(
        telegram_message_id=ts,
        chat_id=chat_id,
        from_user_id=user_id,
        text=text,
        voice_file_id=None,
        timestamp=ts,
    )


def test_bundle_short_followup():
    first = _msg("was machst du", ts=1000)
    second = _msg("ok.", ts=1002)
    assert should_bundle(first, second) is True


def test_no_bundle_different_users():
    first = _msg("hello", ts=1000, user_id=1)
    second = _msg("world", ts=1001, user_id=2)
    assert should_bundle(first, second) is False


def test_no_bundle_time_exceeded():
    first = _msg("hello", ts=1000)
    second = _msg("world", ts=1010)
    assert should_bundle(first, second) is False


def test_bundle_open_ended_message():
    first = _msg("ich brauche hilfe mit...", ts=1000)
    second = _msg("dem Routing-Problem", ts=1003)
    assert should_bundle(first, second) is True


def test_no_bundle_different_chats():
    first = _msg("hi", ts=1000, chat_id=1)
    second = _msg("ho", ts=1001, chat_id=2)
    assert should_bundle(first, second) is False


def test_is_open_ended():
    assert _is_open_ended("text...") is True
    assert _is_open_ended("text,") is True
    assert _is_open_ended("done.") is False


def test_merge_content():
    assert merge_content("hello", "world") == "hello\nworld"
    assert merge_content(None, "world") == "world"
    assert merge_content("hello", None) == "hello"
