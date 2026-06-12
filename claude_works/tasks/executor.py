"""Tool-tag executor — processes read-only TAGs in agent output.

Only GET GitHub calls, READ_EMAIL, GIT_CLONE, PLUGIN_CONFIG_GET, KB_SEARCH,
TOR_RESTART, GET_CONFIG, and SHELL are auto-executed so the agent can process
the data in a follow-up turn. Write operations and output tags (VOICE, MAP,
SEND_EMAIL, BUTTONS, KB_SAVE, BOARD_TASK, ORCHESTRATE, …) are left intact
for the result handler.
"""
import asyncio
import json
import logging
import os
import re
from typing import Awaitable, Callable

import httpx

from .. import config, db
from ..auth import trust as trust_mod
from ..knowledge import store as knowledge_store
from ..tasks import tags as _tags
from ..tasks.email import read_emails as _read_emails
from ..tasks.tor import restart_tor as _restart_tor

logger = logging.getLogger(__name__)

_PLUGINS_DIR = os.environ.get("PLUGINS_DIR", "/data/plugins")
_MAX_TOOL_OUTPUT_CHARS = 4000


async def exec_tool_tags(
    result: str,
    *,
    user_id: int | None = None,
    chat_id: int | None = None,
    deploy_guard_action: Callable[[str], Awaitable[str]],
    track_payloads: Callable[[int | None, list[str]], None],
) -> "tuple[str, str | None]":
    """Execute read-only tool tags. Returns (cleaned_result, tool_output_or_None)."""
    tool_results: list[str] = []

    # ── GitHub GET ────────────────────────────────────────────────────────────
    while True:
        clean, github_args = _tags.extract_github_api(result)
        if not github_args:
            break
        method, endpoint, body = github_args
        if method != "GET":
            break  # write ops stay intact for result handler
        result = clean
        try:
            github_cfg = config.section("github")
            token = github_cfg.get("token", "")
            url = f"https://api.github.com{endpoint}"
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=30.0) as hc:
                resp = await hc.get(url, headers=headers)
            if resp.status_code == 200:
                data_str = json.dumps(resp.json(), ensure_ascii=False, indent=2)
                tool_results.append(f"GitHub GET {endpoint}:\n{data_str[:_MAX_TOOL_OUTPUT_CHARS]}")
            else:
                tool_results.append(f"GitHub GET {endpoint}: HTTP {resp.status_code}")
        except Exception as e:
            logger.warning("GitHub GET %s failed: %s", endpoint, e)
            tool_results.append(f"GitHub GET {endpoint}: request failed")

    # ── READ_EMAIL ────────────────────────────────────────────────────────────
    while True:
        clean, email_args = _tags.extract_read_email(result)
        if not email_args:
            break
        result = clean
        folder, count = email_args
        try:
            emails = await _read_emails(folder, count, config.section("email"))
            lines = [
                f"{i+1}. From: {m['from']}\n   Subject: {m['subject']}\n   {m['date']}"
                for i, m in enumerate(emails)
            ]
            tool_results.append(f"READ_EMAIL {folder} ({len(emails)} emails):\n" + "\n".join(lines))
        except Exception as e:
            logger.warning("READ_EMAIL %s failed: %s", folder, e)
            tool_results.append(f"READ_EMAIL {folder}: request failed")

    # ── GIT_CLONE ─────────────────────────────────────────────────────────────
    clean, git_args = _tags.extract_git_clone(result)
    if git_args:
        repo_url, plugin_name = git_args
        result = clean
        safe_name = re.sub(r'[^a-zA-Z0-9._-]', '', plugin_name)[:64]
        target = f"{_PLUGINS_DIR}/{safe_name}"
        try:
            os.makedirs(_PLUGINS_DIR, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *_tags.build_git_clone_cmd(repo_url, target),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120.0)
            if proc.returncode == 0:
                tool_results.append(f"GIT_CLONE {repo_url} → {target}: success")
            else:
                tool_results.append(f"GIT_CLONE {repo_url}: failed — {stderr.decode(errors='replace')[:300]}")
        except asyncio.TimeoutError:
            tool_results.append(f"GIT_CLONE {repo_url}: timeout (120s)")
        except Exception as e:
            logger.warning("GIT_CLONE %s failed: %s", repo_url, e)
            tool_results.append(f"GIT_CLONE {repo_url}: error")

    # ── PLUGIN_CONFIG_GET ─────────────────────────────────────────────────────
    while True:
        clean, plugin_name = _tags.extract_plugin_config_get(result)
        if not plugin_name:
            break
        result = clean
        plugins = config.get().get("plugins") or {}
        plugin_cfg = plugins.get(plugin_name) if isinstance(plugins, dict) else None
        if plugin_cfg:
            tool_results.append(
                f"PLUGIN_CONFIG_GET '{plugin_name}':\n{json.dumps(plugin_cfg, ensure_ascii=False, indent=2)}"
            )
        else:
            tool_results.append(
                f"PLUGIN_CONFIG_GET '{plugin_name}': not configured (use PLUGIN_CONFIG_SET)"
            )

    # ── KB_SEARCH ─────────────────────────────────────────────────────────────
    while True:
        clean, kb_query = _tags.extract_kb_search(result)
        if not kb_query:
            break
        result = clean
        try:
            conn = await db.get_conn()
            trust = await trust_mod.chat_trust(conn, chat_id, user_id)
            entries = await knowledge_store.search(conn, kb_query, limit=10, trust=trust)
            await conn.close()
            if entries:
                lines = []
                for e in entries:
                    tags = ", ".join(e.get("tags") or [])
                    tag_str = f" [{tags}]" if tags else ""
                    body = e["content"][:400] + ("…" if len(e["content"]) > 400 else "")
                    lines.append(f"- ID:{e['id']} [{e['type']}]{tag_str} **{e['title']}**: {body}")
                tool_results.append(f"KB_SEARCH '{kb_query}' ({len(entries)} results):\n" + "\n".join(lines))
            else:
                tool_results.append(f"KB_SEARCH '{kb_query}': no results found")
        except Exception as e:
            logger.warning("KB_SEARCH failed: %s", e)
            tool_results.append(f"KB_SEARCH '{kb_query}': search failed")

    # ── TOR_RESTART ───────────────────────────────────────────────────────────
    result, found_restart = _tags.extract_tor_restart(result)
    if found_restart:
        status = await _restart_tor()
        tool_results.append(f"TOR_RESTART: {status}")

    # ── GET_CONFIG ────────────────────────────────────────────────────────────
    while True:
        clean, cfg_key = _tags.extract_get_config(result)
        if not cfg_key:
            break
        result = clean
        tool_results.append(_tags.get_config_by_dotpath(cfg_key))

    # ── SHELL ─────────────────────────────────────────────────────────────────
    while True:
        clean, shell_cmd = _tags.extract_shell(result)
        if not shell_cmd:
            break
        result = clean
        if not _tags.shell_allowed(shell_cmd):
            tool_results.append(f"SHELL '{shell_cmd}': blocked — not in whitelist")
            continue
        try:
            proc = await asyncio.create_subprocess_shell(
                shell_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            out = stdout.decode(errors="replace")[:3000]
            tool_results.append(f"SHELL '{shell_cmd}' (rc={proc.returncode}):\n{out}")
        except asyncio.TimeoutError:
            tool_results.append(f"SHELL '{shell_cmd}': timeout (30s)")
        except Exception as e:
            logger.warning("SHELL '%s' failed: %s", shell_cmd, e)
            tool_results.append(f"SHELL '{shell_cmd}': execution failed")

    # ── DEPLOY_STATUS / DEPLOY_TRIGGER ────────────────────────────────────────
    if "[DEPLOY_STATUS]" in result:
        result = result.replace("[DEPLOY_STATUS]", "")
        tool_results.append(f"DEPLOY_STATUS: {await deploy_guard_action('status')}")
    if "[DEPLOY_TRIGGER]" in result:
        result = result.replace("[DEPLOY_TRIGGER]", "")
        tool_results.append(f"DEPLOY_TRIGGER: {await deploy_guard_action('trigger')}")

    track_payloads(chat_id, tool_results)
    return result, "\n\n".join(tool_results) if tool_results else None
