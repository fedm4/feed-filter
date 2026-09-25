"""Fetching feeds and turning their entries into rows.

Polling 59 feeds every half hour is a lot of requests aimed at other people's servers,
so the shape here is deliberately unhurried: a conditional request that usually costs a
304, one source at a time, and a User-Agent that says who is calling.

Failure is expected rather than exceptional. Feeds move, expire and go down, so a source
that fails counts the failure and the run continues; F3 surfaces the count so a feed that
has been dead for a week is visible instead of merely quiet.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Item, Source
from .settings import Settings

log = logging.getLogger(__name__)


class Status(StrEnum):
    FETCHED = "fetched"
    NOT_MODIFIED = "not_modified"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ParsedEntry:
    """One entry, reduced to what an ``Item`` needs."""

    url: str
    title: str
    summary: str | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    source_name: str
    status: Status
    stored: int = 0
    seen: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not Status.FAILED


def url_hash(url: str) -> str:
    """The dedup key: a hash, because SQLite indexes 64 fixed characters far more
    happily than a 2000-character column.

    D4 replaces the input with the canonicalised URL. Until then this hashes the link as
    the feed gave it, which catches a feed republishing the same entry but not the same
    article arriving from two feeds under different tracking parameters.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _published(entry: Any) -> datetime | None:
    """``published_parsed`` as an aware UTC datetime.

    feedparser normalises whatever timezone the feed declared to UTC and hands back a
    naive struct_time, so the tzinfo has to be reattached here. It is not decoration:
    UTCDateTime refuses naive values precisely so that a guess never reaches storage.
    """
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except TypeError, ValueError:
        return None


def parse_entries(body: bytes) -> list[ParsedEntry]:
    """Every usable entry in a feed document.

    An entry with no link is skipped: it cannot be deduplicated, cannot be opened, and
    there is nothing to be done with it. A missing title falls back to the URL, which is
    ugly but keeps an otherwise fine article.
    """
    document = feedparser.parse(body)
    entries = []
    for entry in document.entries:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        summary = entry.get("summary")
        entries.append(
            ParsedEntry(
                url=link,
                title=(entry.get("title") or link).strip(),
                summary=summary.strip() if isinstance(summary, str) and summary.strip() else None,
                published_at=_published(entry),
            )
        )
    return entries


def conditional_headers(source: Source) -> dict[str, str]:
    """Ask for the feed only if it changed since last time.

    This is the whole reason a half-hour poll over 59 feeds is reasonable: most answers
    are a 304 with no body at all.
    """
    headers = {}
    if source.etag:
        headers["if-none-match"] = source.etag
    if source.last_modified:
        headers["if-modified-since"] = source.last_modified
    return headers


def store_entries(session: Session, source: Source, entries: Sequence[ParsedEntry]) -> int:
    """Insert entries not already stored. Returns how many were new.

    Feeds repeat themselves by design -- every poll re-sends the same twenty articles --
    so the common case is that nothing here is new.
    """
    hashes = {url_hash(entry.url): entry for entry in entries}
    if not hashes:
        return 0

    known = set(session.scalars(select(Item.url_hash).where(Item.url_hash.in_(hashes))).all())
    stored = 0
    for digest, entry in hashes.items():
        if digest in known:
            continue
        session.add(
            Item(
                source_id=source.id,
                url=entry.url,
                # D4 canonicalises; until then the link stands for itself.
                canonical_url=entry.url,
                url_hash=digest,
                title=entry.title,
                summary=entry.summary,
                published_at=entry.published_at,
            )
        )
        stored += 1
    session.flush()
    return stored


def poll_source(session: Session, client: httpx.Client, source: Source) -> FetchOutcome:
    """Fetch one source and store whatever is new, updating its fetch state."""
    try:
        response = client.get(source.url, headers=conditional_headers(source))
    except httpx.HTTPError as exc:
        source.consecutive_failures += 1
        detail = f"{type(exc).__name__}"
        log.warning(
            "%s: unreachable (%s), %d in a row", source.name, detail, source.consecutive_failures
        )
        return FetchOutcome(source.name, Status.FAILED, detail=detail)

    if response.status_code == 304:
        # Success, and the cheapest kind: the server confirmed nothing changed without
        # sending the document. The validators stay as they are.
        source.consecutive_failures = 0
        log.info("%s: 304 not modified", source.name)
        return FetchOutcome(source.name, Status.NOT_MODIFIED)

    if response.status_code != 200:
        source.consecutive_failures += 1
        detail = f"HTTP {response.status_code}"
        log.warning("%s: %s, %d in a row", source.name, detail, source.consecutive_failures)
        return FetchOutcome(source.name, Status.FAILED, detail=detail)

    entries = parse_entries(response.content)
    if not entries:
        # Parsed to nothing. feedparser is forgiving enough that reaching here means the
        # document was not a feed at all -- an HTML error page, or a redirect to one.
        source.consecutive_failures += 1
        detail = "no entries"
        log.warning("%s: %s, %d in a row", source.name, detail, source.consecutive_failures)
        return FetchOutcome(source.name, Status.FAILED, detail=detail)

    stored = store_entries(session, source, entries)

    # Only now: validators are recorded once the body they describe has been stored, so
    # a crash between the two costs a re-fetch rather than a silently skipped update.
    source.etag = response.headers.get("etag")
    source.last_modified = response.headers.get("last-modified")
    source.consecutive_failures = 0
    log.info("%s: %d new of %d", source.name, stored, len(entries))
    return FetchOutcome(source.name, Status.FETCHED, stored=stored, seen=len(entries))


def build_client(settings: Settings | None = None, *, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"user-agent": (settings or Settings()).user_agent},
    )


def enabled_sources(session: Session) -> list[Source]:
    return list(session.scalars(select(Source).where(Source.enabled.is_(True)).order_by(Source.id)))


def poll_all(
    session: Session,
    sources: Iterable[Source] | None = None,
    *,
    client: httpx.Client | None = None,
) -> list[FetchOutcome]:
    """Poll every enabled source, one at a time.

    Sequential, which gives per-host serialisation for free and needs no scheduling to
    achieve it. At roughly a second each, 59 feeds take about a minute -- against a
    thirty minute interval there is nothing to win by going faster, and a great deal to
    lose by being the client that hammers someone's blog.
    """
    owned = client is None
    client = client or build_client()
    try:
        return [
            poll_source(session, client, source)
            for source in (sources if sources is not None else enabled_sources(session))
        ]
    finally:
        if owned:
            client.close()
