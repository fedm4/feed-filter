"""Fetching an article's body when the feed only gave a teaser.

Off by default (`FF_FETCH_FULL_TEXT`). It multiplies requests to news sites, and title
plus summary is usually enough to judge tone and opinion-vs-reporting. Turn it on only if
it measurably helps.
"""

from __future__ import annotations

import logging

import httpx
import trafilatura

from .settings import Settings

log = logging.getLogger(__name__)

#: Laya's context window is 1024 tokens. Roughly four characters per token, and the
#: question text needs room too, so the body gets a bit under three quarters of it.
MAX_BODY_CHARS = 3000


def truncate(text: str, limit: int = MAX_BODY_CHARS) -> str:
    """Cut to `limit`, preferring the last sentence break so the model sees whole thoughts."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("\n"))
    return (cut[: stop + 1] if stop > limit // 2 else cut).strip()


def extract_body(html: str | bytes) -> str | None:
    """The article text from a page, or None when there is nothing usable."""
    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    if not text or not text.strip():
        return None
    return truncate(text.strip())


def fetch_body(client: httpx.Client, url: str) -> str | None:
    """Fetch and extract. Returns None on any failure -- the summary stays as fallback."""
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        log.info("full text: %s unreachable (%s)", url, type(exc).__name__)
        return None
    if response.status_code != 200:
        log.info("full text: %s answered %d", url, response.status_code)
        return None
    return extract_body(response.text)


def enabled(settings: Settings | None = None) -> bool:
    return (settings or Settings()).fetch_full_text
