"""Pre-approval whitelist for write operations.

A whitelist rule lets a *specific* write operation skip the security approval
gate (Security-Officer review + human supervisor confirmation). Without a
matching rule every write keeps the existing behaviour: it is reviewed and, for
human-gated actions, queued for supervisor approval.

Scope — four write types, each with its own matcher shape:

  github_merge      matcher: {"repo": "owner/repo", "branch": "feature/*"}
                    A GitHub API write whose endpoint is a merge
                    (`/repos/<repo>/merges` or `/repos/<repo>/pulls/<n>/merge`).
                    `branch` is matched against the *target* (base) branch.

  github_api_write  matcher: {"method": "POST"|"*", "endpoint": "/repos/o/r/*"}
                    Any non-merge GitHub API write.

  send_email        matcher: {"domain": "example.com"}  (or {"recipient": "x@y"})
                    Matched against the recipient address.

  config_put        matcher: {"key_prefix": "tts."}  (or {"key": "tts.voice"})
                    Matched against the dotted config path being written.

Matching is **fail-closed**: if a matcher field references information the
context cannot supply (e.g. the base branch of a PR-number merge), the rule does
*not* match and the operation falls back to the normal approval path.
"""
from __future__ import annotations

import re
from fnmatch import fnmatch

# Write types managed by the whitelist. `config_put` and the github subtypes are
# all real, gated write paths in the daemon.
WRITE_TYPES = ("github_merge", "github_api_write", "send_email", "config_put")

# Maps a coarse security-rule action type to the whitelist write type(s) it may
# correspond to. github_write fans out into merge + generic api write.
_ACTION_TO_WRITE_TYPES = {
    "email_send": ("send_email",),
    "github_write": ("github_merge", "github_api_write"),
    "config_put": ("config_put",),
}

_GITHUB_TAG_RE = re.compile(
    r"\[GITHUB_API:\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*([^\s|\]\n]+)\s*(?:\|\s*([^\]]*))?\]",
    re.IGNORECASE,
)
_EMAIL_TAG_RE = re.compile(r"\[SEND_EMAIL:\s*([^\s|\]]+)", re.IGNORECASE)
_CONFIG_TAG_RE = re.compile(r"\[CONFIG_UPDATE:\s*([^\s|\]]+)", re.IGNORECASE)

_MERGE_ENDPOINT_RE = re.compile(
    r"^/repos/([^/]+/[^/]+)/(?:merges/?$|pulls/\d+/merge/?$)", re.IGNORECASE
)
_REPO_RE = re.compile(r"^/repos/([^/]+/[^/]+)", re.IGNORECASE)

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------
# Context builders — turn raw tag values into a matcher-comparable context dict.
# ---------------------------------------------------------------------------

def classify_github(method: str, endpoint: str) -> str:
    """Return the whitelist write type for a GitHub write op."""
    if _MERGE_ENDPOINT_RE.match((endpoint or "").strip()):
        return "github_merge"
    return "github_api_write"


def github_context(method: str, endpoint: str, body: str | None) -> dict:
    """Build match context for a GitHub write. `branch` is the merge base branch
    when it can be parsed from the body; otherwise it is left absent so that any
    branch-constrained rule fails closed."""
    endpoint = (endpoint or "").strip()
    ctx: dict = {"method": (method or "").upper(), "endpoint": endpoint}
    m = _REPO_RE.match(endpoint)
    if m:
        ctx["repo"] = m.group(1)
    if _MERGE_ENDPOINT_RE.match(endpoint) and body:
        base = _json_field(body, "base")
        head = _json_field(body, "head")
        if base:
            ctx["branch"] = base
        if head:
            ctx["head"] = head
    return ctx


def email_context(recipient: str) -> dict:
    recipient = (recipient or "").strip().lower()
    ctx: dict = {"recipient": recipient}
    if "@" in recipient:
        ctx["domain"] = recipient.rsplit("@", 1)[1]
    return ctx


def config_context(key: str) -> dict:
    return {"key": (key or "").strip()}


def _json_field(body: str, field: str) -> str | None:
    """Cheap extraction of a top-level string field from a JSON-ish body without
    a hard JSON dependency (bodies are sometimes templated)."""
    try:
        import json
        data = json.loads(body)
        if isinstance(data, dict) and isinstance(data.get(field), str):
            return data[field]
    except Exception:
        pass
    m = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]+)"', body)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _matcher_field_matches(rule_value: str, ctx_value, *, glob: bool) -> bool:
    if rule_value in (None, "", "*"):
        return True  # unconstrained
    if ctx_value is None:
        return False  # fail closed: rule constrains a field the context lacks
    rule_value = str(rule_value)
    ctx_value = str(ctx_value)
    if glob:
        return fnmatch(ctx_value, rule_value)
    return rule_value.lower() == ctx_value.lower()


def match_rule(write_type: str, context: dict, rule: dict) -> bool:
    """True if a single rule grants this write."""
    if not rule.get("enabled", True):
        return False
    if rule.get("type") != write_type:
        return False
    matcher = rule.get("matcher") or {}

    if write_type == "github_merge":
        return (
            _matcher_field_matches(matcher.get("repo"), context.get("repo"), glob=True)
            and _matcher_field_matches(matcher.get("branch"), context.get("branch"), glob=True)
        )
    if write_type == "github_api_write":
        return (
            _matcher_field_matches(matcher.get("method"), context.get("method"), glob=False)
            and _matcher_field_matches(matcher.get("endpoint"), context.get("endpoint"), glob=True)
        )
    if write_type == "send_email":
        # A rule may constrain by exact recipient and/or domain; both must hold.
        return (
            _matcher_field_matches(matcher.get("recipient"), context.get("recipient"), glob=True)
            and _matcher_field_matches(matcher.get("domain"), context.get("domain"), glob=True)
        )
    if write_type == "config_put":
        if matcher.get("key"):
            return _matcher_field_matches(matcher.get("key"), context.get("key"), glob=True)
        prefix = matcher.get("key_prefix")
        if prefix in (None, "", "*"):
            return True
        key = context.get("key")
        return bool(key) and str(key).startswith(str(prefix))
    return False


def matches(write_type: str, context: dict, rules: list[dict] | None) -> bool:
    """True if any active rule grants this write."""
    if not rules:
        return False
    return any(match_rule(write_type, context, r) for r in rules)


# ---------------------------------------------------------------------------
# Occurrence extraction (response-gate use)
# ---------------------------------------------------------------------------

def extract_email_recipients(content: str) -> list[str]:
    return [m.group(1).strip() for m in _EMAIL_TAG_RE.finditer(content or "")]


def extract_github_writes(content: str) -> list[tuple[str, str, str | None]]:
    out: list[tuple[str, str, str | None]] = []
    for m in _GITHUB_TAG_RE.finditer(content or ""):
        method = m.group(1).upper()
        if method not in _WRITE_METHODS:
            continue
        out.append((method, m.group(2).strip(), (m.group(3) or "").strip() or None))
    return out


def extract_config_keys(content: str) -> list[str]:
    return [m.group(1).strip() for m in _CONFIG_TAG_RE.finditer(content or "")]


def all_whitelisted(action_type: str, content: str, rules: list[dict] | None) -> bool:
    """True only if EVERY whitelist-relevant occurrence of `action_type` in
    `content` is granted by some rule (and at least one occurrence exists).

    Conservative by design: a response carrying one whitelisted and one
    non-whitelisted write of the same type is NOT fully whitelisted, so the
    normal approval path still runs.
    """
    if action_type not in _ACTION_TO_WRITE_TYPES or not rules:
        return False

    if action_type == "email_send":
        recipients = extract_email_recipients(content)
        if not recipients:
            return False
        return all(matches("send_email", email_context(r), rules) for r in recipients)

    if action_type == "github_write":
        writes = extract_github_writes(content)
        if not writes:
            return False
        for method, endpoint, body in writes:
            wt = classify_github(method, endpoint)
            if not matches(wt, github_context(method, endpoint, body), rules):
                return False
        return True

    if action_type == "config_put":
        keys = extract_config_keys(content)
        if not keys:
            return False
        return all(matches("config_put", config_context(k), rules) for k in keys)

    return False
