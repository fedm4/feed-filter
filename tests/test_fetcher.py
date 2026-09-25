"""Fetcher response cases, on a mocked transport. No network.

The conditional request is the point of most of this: a poll that mostly costs 304s is
what makes half-hourly polling of 59 other people's servers a reasonable thing to do.
"""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from conftest import make_item, make_source
from feedfilter.db import session_scope
from feedfilter.fetcher import (
    Status,
    parse_entries,
    poll_all,
    poll_source,
    url_hash,
)
from feedfilter.models import Item

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Example</title>
  <item>
    <title>First article</title>
    <link>https://example.com/first</link>
    <description>A summary of the first.</description>
    <pubDate>Wed, 24 Sep 2026 14:57:57 GMT</pubDate>
  </item>
  <item>
    <title>Second article</title>
    <link>https://example.com/second</link>
    <pubDate>Wed, 24 Sep 2026 09:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Example</title>
  <entry>
    <title>An atom entry</title>
    <link href="https://example.com/atom-one"/>
    <updated>2026-09-24T14:00:00Z</updated>
  </entry>
</feed>
"""


def responder(status_code=200, body=RSS, headers=None, record=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        if status_code == 304:
            return httpx.Response(304)
        return httpx.Response(status_code, content=body.encode(), headers=headers or {})

    return handler


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------------------ parsing


def test_parses_rss_entries() -> None:
    entries = parse_entries(RSS.encode())

    assert [entry.title for entry in entries] == ["First article", "Second article"]
    assert entries[0].url == "https://example.com/first"
    assert entries[0].summary == "A summary of the first."
    assert entries[1].summary is None


def test_parses_atom_entries() -> None:
    entries = parse_entries(ATOM.encode())

    assert entries[0].url == "https://example.com/atom-one"
    assert entries[0].title == "An atom entry"


def test_published_dates_come_back_aware() -> None:
    """UTCDateTime refuses naive values, so the tzinfo has to survive feedparser."""
    entry = parse_entries(RSS.encode())[0]

    assert entry.published_at == datetime(2026, 9, 24, 14, 57, 57, tzinfo=UTC)
    assert entry.published_at.tzinfo is not None


def test_an_entry_without_a_link_is_skipped() -> None:
    """It cannot be deduplicated and cannot be opened; there is nothing to do with it."""
    body = RSS.replace("<link>https://example.com/second</link>", "")

    entries = parse_entries(body.encode())

    assert [entry.url for entry in entries] == ["https://example.com/first"]


def test_a_missing_title_falls_back_to_the_url() -> None:
    body = RSS.replace("<title>First article</title>", "")

    assert parse_entries(body.encode())[0].title == "https://example.com/first"


def test_html_is_not_a_feed() -> None:
    assert parse_entries(b"<html><body>nope</body></html>") == []


def test_malformed_xml_yields_nothing_rather_than_raising() -> None:
    """feedparser is forgiving, so this must not explode -- it must simply find nothing."""
    assert parse_entries(b"<rss><channel><item><title>unclosed") == []


# ------------------------------------------------------------------------------ polling


def test_a_200_stores_items_and_records_validators(factory) -> None:
    handler = responder(
        headers={"etag": 'W/"v1"', "last-modified": "Wed, 24 Sep 2026 15:00:00 GMT"}
    )

    with session_scope(factory) as session:
        source = make_source(session)
        outcome = poll_source(session, client_for(handler), source)

    assert outcome.status is Status.FETCHED
    assert (outcome.stored, outcome.seen) == (2, 2)
    with session_scope(factory) as session:
        stored = session.scalars(select(Item).order_by(Item.url)).all()
        assert [item.title for item in stored] == ["First article", "Second article"]
        assert stored[0].url_hash == url_hash(stored[0].url)
        refreshed = session.scalars(select(type(source)).where(type(source).id == source.id)).one()
        assert refreshed.etag == 'W/"v1"'
        assert refreshed.last_modified == "Wed, 24 Sep 2026 15:00:00 GMT"


def test_stored_validators_are_sent_back_next_time(factory) -> None:
    seen: list[httpx.Request] = []

    with session_scope(factory) as session:
        source = make_source(session, etag='W/"v1"', last_modified="Wed, 24 Sep 2026 15:00:00 GMT")
        poll_source(session, client_for(responder(304, record=seen)), source)

    assert seen[0].headers["if-none-match"] == 'W/"v1"'
    assert seen[0].headers["if-modified-since"] == "Wed, 24 Sep 2026 15:00:00 GMT"


def test_a_304_is_success_and_stores_nothing(factory) -> None:
    """The cheapest possible poll, and the one that makes the interval affordable."""
    with session_scope(factory) as session:
        source = make_source(session, etag='W/"v1"', consecutive_failures=2)
        outcome = poll_source(session, client_for(responder(304)), source)

    assert outcome.status is Status.NOT_MODIFIED
    assert outcome.ok
    assert outcome.stored == 0
    with session_scope(factory) as session:
        refreshed = session.scalars(select(type(source))).one()
        assert refreshed.consecutive_failures == 0, "a 304 clears the failure streak"
        assert refreshed.etag == 'W/"v1"', "the validator it answered to must survive"
    with session_scope(factory) as session:
        assert session.scalars(select(Item)).all() == []


def test_a_second_poll_of_an_unchanged_feed_stores_nothing(factory) -> None:
    """Feeds resend the same twenty articles every time; that must cost nothing."""
    with session_scope(factory) as session:
        source = make_source(session)
        first = poll_source(session, client_for(responder()), source)
        second = poll_source(session, client_for(responder()), source)

    assert (first.stored, second.stored) == (2, 0)
    assert second.seen == 2
    with session_scope(factory) as session:
        assert len(session.scalars(select(Item)).all()) == 2


def test_an_item_already_stored_by_another_source_is_not_duplicated(factory) -> None:
    with session_scope(factory) as session:
        other = make_source(session)
        make_item(session, other, url_hash=url_hash("https://example.com/first"))
        source = make_source(session)
        outcome = poll_source(session, client_for(responder()), source)

    assert outcome.stored == 1


@pytest.mark.parametrize("status_code", [404, 410, 500, 503])
def test_an_error_response_counts_a_failure(factory, status_code) -> None:
    with session_scope(factory) as session:
        source = make_source(session, consecutive_failures=1)
        outcome = poll_source(session, client_for(responder(status_code)), source)

    assert outcome.status is Status.FAILED
    assert str(status_code) in outcome.detail
    with session_scope(factory) as session:
        assert session.scalars(select(type(source))).one().consecutive_failures == 2


def test_a_timeout_counts_a_failure(factory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with session_scope(factory) as session:
        source = make_source(session)
        outcome = poll_source(session, client_for(handler), source)

    assert outcome.status is Status.FAILED
    assert "Timeout" in outcome.detail
    with session_scope(factory) as session:
        assert session.scalars(select(type(source))).one().consecutive_failures == 1


def test_a_200_that_is_not_a_feed_counts_a_failure(factory) -> None:
    """A redirect to an HTML error page answers 200 and parses to nothing."""
    with session_scope(factory) as session:
        source = make_source(session)
        outcome = poll_source(session, client_for(responder(body="<html>gone</html>")), source)

    assert outcome.status is Status.FAILED
    assert outcome.detail == "no entries"
    with session_scope(factory) as session:
        assert session.scalars(select(type(source))).one().consecutive_failures == 1


def test_a_failure_leaves_the_old_validators_alone(factory) -> None:
    """Otherwise one bad response would force a full refetch of a feed that is fine."""
    with session_scope(factory) as session:
        source = make_source(session, etag='W/"v1"')
        poll_source(session, client_for(responder(503)), source)

    with session_scope(factory) as session:
        assert session.scalars(select(type(source))).one().etag == 'W/"v1"'


def test_poll_all_skips_disabled_sources(factory) -> None:
    with session_scope(factory) as session:
        make_source(session, name="On")
        make_source(session, name="Off", enabled=False)
        outcomes = poll_all(session, client=client_for(responder()))

    assert [outcome.source_name for outcome in outcomes] == ["On"]


def test_one_failing_source_does_not_stop_the_run(factory) -> None:
    """A dead feed is an ordinary Tuesday, not a reason to abandon the other 58."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, content=RSS.encode())

    with session_scope(factory) as session:
        make_source(session, name="Broken")
        make_source(session, name="Fine")
        outcomes = poll_all(session, client=client_for(handler))

    assert [outcome.status for outcome in outcomes] == [Status.FAILED, Status.FETCHED]
    with session_scope(factory) as session:
        assert len(session.scalars(select(Item)).all()) == 2


def test_the_user_agent_names_the_software_not_the_person() -> None:
    """A contact URL here would put one operator's identity in 59 servers' logs."""
    from feedfilter.fetcher import build_client
    from feedfilter.settings import Settings

    with build_client(Settings()) as client:
        agent = client.headers["user-agent"]

    assert agent == "feed-filter/0.1"
    assert "github.com" not in agent
    # Unidentified is fine; disguised is not.
    assert "Mozilla" not in agent


def test_the_user_agent_is_configurable() -> None:
    from feedfilter.fetcher import build_client
    from feedfilter.settings import Settings

    with build_client(Settings(user_agent="feed-filter/0.1 (+mailto:me@example.com)")) as client:
        assert client.headers["user-agent"] == "feed-filter/0.1 (+mailto:me@example.com)"
