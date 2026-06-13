"""Shared HTTP fetcher with browser-like headers for external web requests."""
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_MAX_FETCH_CHARS = 12_000

# Firefox 128 ESR on Windows — realistic browser fingerprint
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de,en-US;q=0.7,en;q=0.3",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


async def fetch_url_content(url: str, proxy: str | None = None) -> str | None:
    """Fetch URL content with Firefox-like headers. Returns stripped plain text or None."""
    try:
        client_kwargs: dict = {"timeout": 15.0, "follow_redirects": True}
        if proxy:
            client_kwargs["proxy"] = proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url, headers=BROWSER_HEADERS)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "text" not in ct and "json" not in ct:
                return None
            text = resp.text
        text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&(?:[a-z]+|#\d+);", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:_MAX_FETCH_CHARS] if text else None
    except Exception as e:
        logger.debug("URL fetch failed proxy=%s (%s): %s", proxy, url, e)
        return None
