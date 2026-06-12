"""Unit tests for the pure whitelist rule-evaluation module."""
from claude_works.security import whitelist as wl


# --- match_rule / matches ---------------------------------------------------

def test_github_merge_repo_and_branch_glob():
    rule = {"type": "github_merge", "matcher": {"repo": "mirkofelt/claude-works", "branch": "feature/*"}, "enabled": True}
    ctx = {"repo": "mirkofelt/claude-works", "branch": "feature/login"}
    assert wl.match_rule("github_merge", ctx, rule) is True
    assert wl.matches("github_merge", ctx, [rule]) is True


def test_github_merge_branch_glob_miss():
    rule = {"type": "github_merge", "matcher": {"repo": "o/r", "branch": "feature/*"}, "enabled": True}
    assert wl.match_rule("github_merge", {"repo": "o/r", "branch": "main"}, rule) is False


def test_github_merge_repo_miss():
    rule = {"type": "github_merge", "matcher": {"repo": "o/r", "branch": "*"}, "enabled": True}
    assert wl.match_rule("github_merge", {"repo": "other/repo", "branch": "x"}, rule) is False


def test_fail_closed_when_branch_missing_from_context():
    # Rule constrains branch but the context could not determine it → no match.
    rule = {"type": "github_merge", "matcher": {"repo": "o/r", "branch": "feature/*"}, "enabled": True}
    assert wl.match_rule("github_merge", {"repo": "o/r"}, rule) is False


def test_unconstrained_branch_matches_any():
    rule = {"type": "github_merge", "matcher": {"repo": "o/r"}, "enabled": True}
    assert wl.match_rule("github_merge", {"repo": "o/r"}, rule) is True


def test_github_api_write_method_and_endpoint():
    rule = {"type": "github_api_write", "matcher": {"method": "POST", "endpoint": "/repos/o/r/*"}, "enabled": True}
    assert wl.match_rule("github_api_write", {"method": "POST", "endpoint": "/repos/o/r/issues"}, rule) is True
    assert wl.match_rule("github_api_write", {"method": "DELETE", "endpoint": "/repos/o/r/issues"}, rule) is False


def test_github_api_write_method_wildcard():
    rule = {"type": "github_api_write", "matcher": {"method": "*", "endpoint": "/repos/o/r/*"}, "enabled": True}
    assert wl.match_rule("github_api_write", {"method": "PATCH", "endpoint": "/repos/o/r/x"}, rule) is True


def test_send_email_domain():
    rule = {"type": "send_email", "matcher": {"domain": "example.com"}, "enabled": True}
    assert wl.matches("send_email", wl.email_context("alice@example.com"), [rule]) is True
    assert wl.matches("send_email", wl.email_context("bob@other.com"), [rule]) is False


def test_send_email_recipient_and_domain_both_required():
    rule = {"type": "send_email", "matcher": {"domain": "example.com", "recipient": "alice@example.com"}, "enabled": True}
    assert wl.matches("send_email", wl.email_context("alice@example.com"), [rule]) is True
    assert wl.matches("send_email", wl.email_context("eve@example.com"), [rule]) is False


def test_config_put_key_prefix():
    rule = {"type": "config_put", "matcher": {"key_prefix": "tts."}, "enabled": True}
    assert wl.matches("config_put", wl.config_context("tts.voice"), [rule]) is True
    assert wl.matches("config_put", wl.config_context("llm.api_key"), [rule]) is False


def test_config_put_exact_key():
    rule = {"type": "config_put", "matcher": {"key": "tts.voice"}, "enabled": True}
    assert wl.matches("config_put", wl.config_context("tts.voice"), [rule]) is True
    assert wl.matches("config_put", wl.config_context("tts.speed"), [rule]) is False


def test_disabled_rule_never_matches():
    rule = {"type": "send_email", "matcher": {"domain": "example.com"}, "enabled": False}
    assert wl.matches("send_email", wl.email_context("a@example.com"), [rule]) is False


def test_type_mismatch():
    rule = {"type": "send_email", "matcher": {"domain": "example.com"}, "enabled": True}
    assert wl.match_rule("config_put", {"key": "x"}, rule) is False


def test_no_rules():
    assert wl.matches("send_email", wl.email_context("a@example.com"), []) is False
    assert wl.matches("send_email", wl.email_context("a@example.com"), None) is False


# --- context builders / classify -------------------------------------------

def test_classify_github_merge_variants():
    assert wl.classify_github("PUT", "/repos/o/r/pulls/3/merge") == "github_merge"
    assert wl.classify_github("POST", "/repos/o/r/merges") == "github_merge"
    assert wl.classify_github("POST", "/repos/o/r/issues") == "github_api_write"


def test_github_context_parses_base_branch_from_body():
    ctx = wl.github_context("POST", "/repos/o/r/merges", '{"base": "feature/x", "head": "fix/y"}')
    assert ctx["repo"] == "o/r"
    assert ctx["branch"] == "feature/x"
    assert ctx["head"] == "fix/y"


def test_github_context_pr_merge_has_no_branch():
    ctx = wl.github_context("PUT", "/repos/o/r/pulls/3/merge", None)
    assert ctx["repo"] == "o/r"
    assert "branch" not in ctx  # fails closed for branch-constrained rules


def test_email_context_lowercases_and_splits_domain():
    ctx = wl.email_context("Alice@Example.COM")
    assert ctx["recipient"] == "alice@example.com"
    assert ctx["domain"] == "example.com"


# --- all_whitelisted (response-gate) ---------------------------------------

EMAIL_RULE = {"type": "send_email", "matcher": {"domain": "example.com"}, "enabled": True}
MERGE_RULE = {"type": "github_merge", "matcher": {"repo": "o/r", "branch": "feature/*"}, "enabled": True}


def test_all_whitelisted_email_all_covered():
    content = "ok [SEND_EMAIL: a@example.com | Hi | body] and [SEND_EMAIL: b@example.com | Yo | x]"
    assert wl.all_whitelisted("email_send", content, [EMAIL_RULE]) is True


def test_all_whitelisted_email_mixed_not_covered():
    content = "[SEND_EMAIL: a@example.com | Hi | b] [SEND_EMAIL: c@evil.com | Hi | b]"
    assert wl.all_whitelisted("email_send", content, [EMAIL_RULE]) is False


def test_all_whitelisted_no_occurrence_is_false():
    assert wl.all_whitelisted("email_send", "no tags here", [EMAIL_RULE]) is False


def test_all_whitelisted_github_merge():
    content = '[GITHUB_API: PUT | /repos/o/r/merges | {"base": "feature/login", "head": "x"}]'
    assert wl.all_whitelisted("github_write", content, [MERGE_RULE]) is True


def test_all_whitelisted_github_merge_wrong_branch():
    content = '[GITHUB_API: PUT | /repos/o/r/merges | {"base": "main", "head": "x"}]'
    assert wl.all_whitelisted("github_write", content, [MERGE_RULE]) is False
