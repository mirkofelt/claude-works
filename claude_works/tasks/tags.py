"""TAG extraction functions for agent output parsing.

Each function returns (clean_text, parsed_value_or_None).
The clean_text has the matched tag removed and whitespace normalized.
"""
import json
import os
import re

from .. import config

# ── Constants ────────────────────────────────────────────────────────────────

PLUGINS_DIR = os.environ.get("PLUGINS_DIR", "/data/plugins")

CONFIG_UPDATE_BLOCKED = {"telegram.token", "web.auth_token", "llm.api_key"}

_SHELL_WHITELIST = re.compile(
    r'^('
    r'git (status|branch|log|fetch|pull|push|diff|show|remote|tag|stash|clone|checkout|merge|rebase|reset|rev-parse|describe)(\s.*)?'
    r'|docker (ps|images|logs|inspect|stats|version)(\s.*)?'
    r'|ls(\s.*)?|pwd|whoami|uname(\s.*)?|df(\s.*)?|free(\s.*)?|uptime'
    r'|cat /proc/version|hostname'
    r')$',
    re.IGNORECASE,
)

_SECRET_KEY_RE = re.compile(r'(key|token|password|secret|passwd|credential|auth)', re.IGNORECASE)

_TOR_RESTART_RE = re.compile(r'\[TOR_RESTART\]', re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean(text: str, m: re.Match) -> str:
    return (text[:m.start()].rstrip() + "\n" + text[m.end():].lstrip()).strip()


def kb_write_allowed(trust: int) -> bool:
    """Only owner (0) and trusted (1) may write to KB directly."""
    return trust <= 1


def shell_allowed(cmd: str) -> bool:
    extra = config.section("shell").get("whitelist", [])
    return bool(_SHELL_WHITELIST.match(cmd) or any(re.match(p, cmd) for p in extra))


def redact_config_value(key: str, value: object) -> object:
    if _SECRET_KEY_RE.search(key) and isinstance(value, str) and value:
        return "<redacted>"
    return value


def get_config_by_dotpath(key: str) -> str:
    parts = key.split(".")
    node: object = config.get()
    for part in parts:
        if not isinstance(node, dict):
            return f"GET_CONFIG '{key}': path not found ('{part}' is not a dict)"
        node = node.get(part)
        if node is None:
            return f"GET_CONFIG '{key}': not set"
    display = redact_config_value(parts[-1], node)
    if isinstance(display, (dict, list)):
        display = json.dumps(display, ensure_ascii=False, indent=2)
    return f"GET_CONFIG '{key}': {display}"


def build_git_clone_cmd(repo_url: str, target: str) -> list[str]:
    tor_proxy = config.section("security").get("tor_socks_proxy", "")
    if tor_proxy:
        git_proxy = tor_proxy.replace("socks5://", "socks5h://")
        return ["git", "-c", f"http.proxy={git_proxy}", "clone", "--depth=1", repo_url, target]
    return ["git", "clone", "--depth=1", repo_url, target]


# ── Extractors ────────────────────────────────────────────────────────────────

def extract_voice(text: str) -> "tuple[str, str | None]":
    m = re.search(r'\[VOICE:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    return _clean(text, m), m.group(1).strip()


def extract_map(text: str) -> "tuple[str, str | None]":
    m = re.search(r'\[MAP:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    return _clean(text, m), m.group(1).strip()


def parse_buttons(text: str) -> "tuple[str, list[list[dict]] | None]":
    """Extract [BUTTONS: label1|data1, label2|data2, ...]. Rows of max 3."""
    m = re.search(r'\[BUTTONS:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    clean = (text[:m.start()].rstrip() + text[m.end():]).strip()
    specs = [s.strip() for s in m.group(1).split(',')]
    buttons = []
    for spec in specs:
        parts = spec.split('|', 1)
        label = parts[0].strip()
        data = parts[1].strip() if len(parts) > 1 else label
        buttons.append({"text": label, "callback_data": data[:64]})
    rows = [buttons[i:i+3] for i in range(0, len(buttons), 3)]
    return clean, rows


def extract_send_email(text: str) -> "tuple[str, tuple[str, str, str] | None]":
    m = re.search(r'\[SEND_EMAIL:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    parts = [p.strip() for p in m.group(1).split('|', 2)]
    if len(parts) < 2:
        return text, None
    body = parts[2] if len(parts) > 2 else ""
    return _clean(text, m), (parts[0], parts[1], body)


def extract_read_email(text: str) -> "tuple[str, tuple[str, int] | None]":
    m = re.search(r'\[READ_EMAIL:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    parts = [p.strip() for p in m.group(1).split('|', 1)]
    folder = parts[0] or "INBOX"
    try:
        count = int(parts[1]) if len(parts) > 1 else 5
    except ValueError:
        count = 5
    return _clean(text, m), (folder, min(count, 20))


def extract_github_api(text: str) -> "tuple[str, tuple[str, str, str] | None]":
    m = re.search(r'\[GITHUB_API:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    parts = [p.strip() for p in m.group(1).split('|', 2)]
    if len(parts) < 2:
        return text, None
    body = parts[2] if len(parts) > 2 else ""
    return _clean(text, m), (parts[0].upper(), parts[1], body)


def extract_git_clone(text: str) -> "tuple[str, tuple[str, str] | None]":
    m = re.search(r'\[GIT_CLONE:\s*([^\]|]+?)\s*\|\s*([^\]]+?)\s*\]', text)
    if not m:
        return text, None
    return _clean(text, m), (m.group(1).strip(), m.group(2).strip())


def extract_mute(text: str) -> "tuple[str, tuple[str, int] | None]":
    m = re.search(r'\[MUTE:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    parts = [p.strip() for p in m.group(1).split('|', 1)]
    ident = parts[0]
    try:
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        minutes = 0
    return _clean(text, m), (ident, minutes)


def extract_unmute(text: str) -> "tuple[str, str | None]":
    m = re.search(r'\[UNMUTE:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    return _clean(text, m), m.group(1).strip()


def extract_get_config(text: str) -> "tuple[str, str | None]":
    m = re.search(r'\[GET_CONFIG:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    return _clean(text, m), m.group(1).strip()


def extract_shell(text: str) -> "tuple[str, str | None]":
    m = re.search(r'\[SHELL:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    return _clean(text, m), m.group(1).strip()


def extract_board_task(text: str) -> "tuple[str, str | None]":
    m = re.search(r'\[BOARD_TASK:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    return _clean(text, m), m.group(1).strip()


def extract_orchestrate(text: str) -> "tuple[str, tuple[str, list[str]] | None]":
    """Extract [ORCHESTRATE: project_name | task1\\ntask2\\n...]."""
    m = re.search(r'\[ORCHESTRATE:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    parts = m.group(1).split('|', 1)
    if len(parts) < 2:
        return text, None
    project_name = parts[0].strip()
    tasks = [t.strip() for t in re.split(r'\n|;', parts[1]) if t.strip()]
    if not tasks:
        return text, None
    return _clean(text, m), (project_name, tasks)


def extract_kb_search(text: str) -> "tuple[str, str | None]":
    m = re.search(r'\[KB_SEARCH:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    return _clean(text, m), m.group(1).strip()


def extract_kb_save(text: str) -> "tuple[str, tuple[str, str, list, str] | None]":
    m = re.search(r'\[KB_SAVE:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    parts = [p.strip() for p in m.group(1).split('|', 3)]
    if not parts[0]:
        return text, None
    title = parts[0]
    entry_type = parts[1] if len(parts) > 1 and parts[1] else "note"
    raw_tags = parts[2] if len(parts) > 2 else ""
    content = parts[3] if len(parts) > 3 else ""
    tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
    return _clean(text, m), (title, entry_type, tags, content)


def extract_kb_update(text: str) -> "tuple[str, tuple | None]":
    m = re.search(r'\[KB_UPDATE:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    parts = [p.strip() for p in m.group(1).split('|', 4)]
    try:
        entry_id = int(parts[0])
    except (ValueError, IndexError):
        return text, None
    title = parts[1] if len(parts) > 1 and parts[1] else None
    entry_type = parts[2] if len(parts) > 2 and parts[2] else None
    raw_tags = parts[3] if len(parts) > 3 else None
    tags = [t.strip() for t in raw_tags.split(',') if t.strip()] if raw_tags else None
    content = parts[4] if len(parts) > 4 and parts[4] else None
    return _clean(text, m), (entry_id, title, entry_type, tags, content)


def extract_config_update(text: str) -> "tuple[str, tuple[str, str] | None]":
    m = re.search(r'\[CONFIG_UPDATE:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    parts = [p.strip() for p in m.group(1).split('|', 1)]
    if len(parts) < 2:
        return text, None
    return _clean(text, m), (parts[0], parts[1])


def extract_plugin_config_get(text: str) -> "tuple[str, str | None]":
    m = re.search(r'\[PLUGIN_CONFIG_GET:\s*([^\]]+)\]', text)
    if not m:
        return text, None
    return _clean(text, m), m.group(1).strip()


def extract_plugin_config_set(text: str) -> "tuple[str, tuple[str, dict] | None]":
    m = re.search(r'\[PLUGIN_CONFIG_SET:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    parts = [p.strip() for p in m.group(1).split('|', 1)]
    if len(parts) < 2:
        return text, None
    try:
        cfg_dict = json.loads(parts[1])
        if not isinstance(cfg_dict, dict):
            return text, None
    except Exception:
        return text, None
    return _clean(text, m), (parts[0], cfg_dict)


def extract_tor_restart(text: str) -> "tuple[str, bool]":
    clean, n = _TOR_RESTART_RE.subn("", text)
    return clean.strip(), n > 0


def extract_remind(text: str) -> "tuple[str, tuple[str, str] | None]":
    """Extract [REMIND: datetime | message]. Returns (clean_text, (datetime_str, message) or None).

    datetime_str formats accepted: ISO 8601, 'YYYY-MM-DD HH:MM', 'HH:MM' (today), '+Xm/h/d' (relative).
    """
    m = re.search(r'\[REMIND:\s*([^\]]+)\]', text, re.DOTALL)
    if not m:
        return text, None
    parts = [p.strip() for p in m.group(1).split('|', 1)]
    if len(parts) < 2 or not parts[0]:
        return text, None
    return _clean(text, m), (parts[0], parts[1])
