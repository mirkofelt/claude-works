import json
import logging

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"


async def github_api(method: str, endpoint: str, body: str | None, cfg: dict) -> dict:
    token = cfg["personal_access_token"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = _GITHUB_API_BASE + endpoint if not endpoint.startswith("http") else endpoint
    payload = None
    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("GitHub API body not valid JSON: %r", body[:80])

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(method.upper(), url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json() if resp.text else {}
