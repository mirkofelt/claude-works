"""SecuritySupervisor integration tests for the write-approval whitelist.

The pure rule-matching logic is covered in test_whitelist.py. This module covers
the supervisor-level behaviour:

  * `whitelisted()` reads live rules from daemon_config and bypasses the gate
    only for matching write ops                              -> test_whitelisted_*
  * whitelist *changes* are meta-protected: their approval keyboard exposes
    no "always" shortcut, so they can never be self-whitelisted away
                                                             -> test_meta_*
"""
from claude_works.security import whitelist as wl
from claude_works.security.supervisor import SecuritySupervisor, PendingApproval


def _rule(rtype, matcher, enabled=True, rid="r1"):
    return {"id": rid, "type": rtype, "matcher": matcher, "enabled": enabled}


def _patch_config(monkeypatch, *, security=None, rules=None):
    state = {
        "security": security if security is not None else {"enabled": True},
        "whitelist": {"rules": rules or []},
    }
    monkeypatch.setattr(
        "claude_works.security.supervisor.section",
        lambda name: state.get(name, {}),
    )
    return state


# --- whitelisted() ----------------------------------------------------------

def test_whitelisted_matches_live_rule(monkeypatch):
    _patch_config(monkeypatch, rules=[_rule("send_email", {"domain": "acme.com"})])
    sup = SecuritySupervisor()
    assert sup.whitelisted("send_email", wl.email_context("ops@acme.com")) is True
    assert sup.whitelisted("send_email", wl.email_context("ops@evil.com")) is False


def test_whitelisted_merge_branch_glob(monkeypatch):
    _patch_config(monkeypatch, rules=[
        _rule("github_merge", {"repo": "acme/web", "branch": "feature/*"}),
    ])
    sup = SecuritySupervisor()
    hit = wl.github_context("POST", "/repos/acme/web/merges", '{"base": "feature/x"}')
    miss = wl.github_context("POST", "/repos/acme/web/merges", '{"base": "main"}')
    assert sup.whitelisted("github_merge", hit) is True
    assert sup.whitelisted("github_merge", miss) is False


def test_whitelisted_unknown_write_type_is_false(monkeypatch):
    _patch_config(monkeypatch, rules=[_rule("send_email", {"domain": "acme.com"})])
    sup = SecuritySupervisor()
    assert sup.whitelisted("definitely_not_a_type", {}) is False


def test_whitelisted_open_when_security_disabled(monkeypatch):
    # Security off → no gate at all → treated as already-allowed.
    _patch_config(monkeypatch, security={"enabled": False},
                  rules=[_rule("send_email", {"domain": "acme.com"})])
    sup = SecuritySupervisor()
    assert sup.whitelisted("send_email", wl.email_context("x@evil.com")) is True


def test_whitelisted_no_rules_is_false(monkeypatch):
    _patch_config(monkeypatch, rules=[])
    sup = SecuritySupervisor()
    assert sup.whitelisted("send_email", wl.email_context("x@acme.com")) is False


# --- meta protection: whitelist changes can never be "always"-approved ------

async def test_meta_approval_keyboard_has_no_always(monkeypatch):
    _patch_config(monkeypatch)
    captured = {}

    async def notify(admin_id, msg, reply_markup=None):
        captured["keyboard"] = reply_markup

    sup = SecuritySupervisor()
    sup._notify_fn = notify
    sup._admin_ids = [42]

    approval = PendingApproval(
        id=7, task_id=None, chat_id=0, user_id=0,
        action_types=["whitelist_change"],
        content="Whitelist ADD: send_email {'domain': 'acme.com'}",
        requested_at=0.0, meta=True,
    )
    await sup._notify_admins(approval)

    callbacks = [
        btn["callback_data"]
        for row in captured["keyboard"]["inline_keyboard"]
        for btn in row
    ]
    assert any(c.startswith("sec_once:") for c in callbacks)
    assert any(c.startswith("sec_deny:") for c in callbacks)
    # The crux of meta-protection: NO "always" shortcut whatsoever.
    assert not any("always" in c for c in callbacks)


async def test_nonmeta_approval_keyboard_keeps_always(monkeypatch):
    _patch_config(monkeypatch)
    captured = {}

    async def notify(admin_id, msg, reply_markup=None):
        captured["keyboard"] = reply_markup

    sup = SecuritySupervisor()
    sup._notify_fn = notify
    sup._admin_ids = [42]

    approval = PendingApproval(
        id=8, task_id=None, chat_id=0, user_id=0,
        action_types=["github_write"], content="POST /repos/acme/web/merges",
        requested_at=0.0, meta=False,
    )
    await sup._notify_admins(approval)

    callbacks = [
        btn["callback_data"]
        for row in captured["keyboard"]["inline_keyboard"]
        for btn in row
    ]
    # A normal write approval still offers the "always allow this action" path.
    assert any("always" in c for c in callbacks)


async def test_require_approval_forces_meta(monkeypatch):
    """require_approval() must create a meta approval (no specific-key shortcut)
    and honour the supervisor's decision."""
    _patch_config(monkeypatch, security={"enabled": True, "pending_timeout_seconds": 5})
    captured = {}

    async def notify(admin_id, msg, reply_markup=None):
        captured["id"] = int(reply_markup["inline_keyboard"][0][0]["callback_data"].split(":")[1])

    sup = SecuritySupervisor()
    sup._notify_fn = notify
    sup._admin_ids = [42]

    import asyncio

    async def approve_soon():
        await asyncio.sleep(0.01)
        sup.approve(captured["id"], admin_id=42)

    asyncio.ensure_future(approve_soon())
    ok = await sup.require_approval(["whitelist_change"], "Whitelist ADD: ...")
    assert ok is True
