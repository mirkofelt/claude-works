import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)


def _fix_json_newlines(s: str) -> str:
    """Escape unescaped newlines/carriage-returns inside JSON string literals."""
    result = []
    in_string = False
    escape_next = False
    for ch in s:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == '\\' and in_string:
            result.append(ch)
            escape_next = True
        elif ch == '"':
            result.append(ch)
            in_string = not in_string
        elif ch == '\n' and in_string:
            result.append('\\n')
        elif ch == '\r' and in_string:
            result.append('\\r')
        else:
            result.append(ch)
    return ''.join(result)


async def github_api(method: str, endpoint: str, body: str | None, cfg: dict) -> dict:
    """Execute a GitHub API call via the gh CLI. Requires gh binary in PATH or cfg.gh_binary."""
    binary = cfg.get("gh_binary", "gh")
    token = cfg.get("token", "")

    cmd = [binary, "api", "--method", method.upper(), endpoint]
    if body:
        try:
            json.loads(body)
        except json.JSONDecodeError:
            body = _fix_json_newlines(body)
            try:
                json.loads(body)
            except json.JSONDecodeError as e:
                raise ValueError(f"GitHub API body is not valid JSON: {e}") from e
        cmd += ["--input", "-"]

    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if body else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdin_data = body.encode() if body else None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=stdin_data), timeout=30.0)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeError("gh api timed out after 30s") from exc

    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed ({proc.returncode}): {stderr.decode()[:400]}")

    return json.loads(stdout.decode()) if stdout.strip() else {}
