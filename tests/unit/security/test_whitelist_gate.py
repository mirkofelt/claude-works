"""Tests for SecuritySupervisor whitelist bypass + meta-protected approval."""
import pytest
from unittest.mock import AsyncMock

import claude_works.config as config
from claude_works.security.rules import build_rules
from claude_works.security.supervisor import SecuritySupervisor, PendingApproval


MERGE_RULE = {"type": "github_merge", "matcher": {"repo": "o/r", "branch": "feature/*"}, "enabled": True}
EMAIL_RULE = {"type": "send_email", "matcher": {"domain": "example.com"}, "enabled": True}


def _make_supervisor(whitelist_rules):
    config.set({
        "security": {"enabled": True},
        "whitelist": {"rules": whitelist_rules},
    })
    sup = SecuritySupervisor()
    sup._rules = build_rules(None)  # default rules: github_write, email_send, ...
    return sup


# --- whitelisted() ----------------------------------------------------------

def test_whitelisted_true_for_matching_rule():
    sup = _make_supervisor([MERGE_RULE])
    assert sup.whitelisted("github_merge", {"repo": "o/r", "branch": "feature/x"}) is True


def test_whitelisted_false_for_non_matching():
    sup = _make_supervisor([MERGE_RULE])
    assert sup.whitelisted("github_merge", {"repo": "o/r", "branch": "main"}) is False


def test_whitelisted_unknown_type_is_false():
    sup = _make_supervisor([MERGE_RULE])
    assert sup.whitelisted("whitelist_change", {}) is False


# --- check() response gate --------------------------------------------------

@pytest.mark.asyncio
async def test_matching_merge_skips_approval_and_so():
    sup = _make_supervisor([MERGE_RULE])
    sup._request_approval = AsyncMock(return_value=True)
    sup._run_so_check = AsyncMock(return_value=True)
    content = '[GITHUB_API: PUT | /repos/o/r/merges | {"base": "feature/login", "head": "x"}]'
    assert await sup.check(content) is True
    sup._request_approval.assert_not_awaited()
    sup._run_so_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_matching_merge_requires_approval():
    sup = _make_supervisor([MERGE_RULE])
    sup._request_approval = AsyncMock(return_value=True)
    sup._run_so_check = AsyncMock(return_value=True)
    content = '[GITHUB_API: PUT | /repos/o/r/merges | {"base": "main", "head": "x"}]'
    assert await sup.check(content) is True
    sup._request_approval.assert_awaited_once()


@pytest.mark.asyncio
async def test_whitelisted_email_skips_approval():
    sup = _make_supervisor([EMAIL_RULE])
    sup._request_approval = AsyncMock(return_value=True)
    sup._run_so_check = AsyncMock(return_value=True)
    content = "[SEND_EMAIL: a@example.com | Subj | Body]"
    assert await sup.check(content) is True
    sup._request_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_domain_email_requires_approval():
    sup = _make_supervisor([EMAIL_RULE])
    sup._request_approval = AsyncMock(return_value=True)
    sup._run_so_check = AsyncMock(return_value=True)
    content = "[SEND_EMAIL: a@evil.com | Subj | Body]"
    assert await sup.check(content) is True
    sup._request_approval.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_recipients_not_fully_whitelisted():
    sup = _make_supervisor([EMAIL_RULE])
    sup._request_approval = AsyncMock(return_value=True)
    sup._run_so_check = AsyncMock(return_value=True)
    content = "[SEND_EMAIL: a@example.com | S | B] [SEND_EMAIL: b@evil.com | S | B]"
    assert await sup.check(content) is True
    sup._request_approval.assert_awaited_once()


# --- meta-protected approval ------------------------------------------------

@pytest.mark.asyncio
async def test_require_approval_is_meta():
    sup = _make_supervisor([])
    captured = {}

    async def fake(action_types, content, *a, meta=False, **kw):
        captured["meta"] = meta
        captured["types"] = action_types
        return True

    sup._request_approval = fake
    ok = await sup.require_approval(["whitelist_change"], "Whitelist ADD: ...")
    assert ok is True
    assert captured["meta"] is True
    assert captured["types"] == ["whitelist_change"]


@pytest.mark.asyncio
async def test_meta_approval_has_no_always_buttons():
    sup = _make_supervisor([])
    sup._admin_ids = [42]
    sent = {}

    async def notify(admin_id, msg, reply_markup=None):
        sent["markup"] = reply_markup

    sup._notify_fn = notify
    approval = PendingApproval(
        id=1, task_id=None, chat_id=0, user_id=0,
        action_types=["whitelist_change"], content="x", requested_at=0.0, meta=True,
    )
    await sup._notify_admins(approval)
    flat = [b["callback_data"] for row in sent["markup"]["inline_keyboard"] for b in row]
    assert any(c.startswith("sec_once") for c in flat)
    assert any(c.startswith("sec_deny") for c in flat)
    assert not any("always" in c for c in flat)


@pytest.mark.asyncio
async def test_normal_approval_keeps_always_buttons():
    sup = _make_supervisor([])
    sup._admin_ids = [42]
    sent = {}

    async def notify(admin_id, msg, reply_markup=None):
        sent["markup"] = reply_markup

    sup._notify_fn = notify
    approval = PendingApproval(
        id=2, task_id=None, chat_id=0, user_id=0,
        action_types=["email_send"], content="[SEND_EMAIL: a@x.de | s | b]", requested_at=0.0,
    )
    await sup._notify_admins(approval)
    flat = [b["callback_data"] for row in sent["markup"]["inline_keyboard"] for b in row]
    assert any("always" in c for c in flat)
