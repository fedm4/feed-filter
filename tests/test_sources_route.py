"""The sources page: toggling, adding, and importing OPML."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import make_source
from feedfilter.db import session_scope
from feedfilter.main import create_app
from feedfilter.models import Source
from feedfilter.opml import OpmlError, parse_opml
from feedfilter.routes.sources import DEAD_AFTER

#: app_env boots with a one-entry catalogue, so the table is never empty to begin with.
SEEDED_URL = "https://seeded.example/feed"


def urls(client) -> set[str]:
    with session_scope(client.app.state.session_factory) as session:
        return {source.url for source in session.scalars(select(Source))} - {SEEDED_URL}


OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>Subscriptions</title></head>
  <body>
    <outline text="Tech">
      <outline type="rss" text="Lobsters" title="Lobsters" xmlUrl="https://lobste.rs/rss"/>
      <outline type="rss" text="LWN" xmlUrl="https://lwn.net/headlines/newrss"/>
    </outline>
    <outline text="Empty folder"/>
    <outline type="rss" text="Top level" xmlUrl="https://example.com/feed"/>
  </body>
</opml>
"""


@pytest.fixture
def client(app_env):
    app = create_app()
    with TestClient(app) as started:
        started.app = app
        yield started


# ----------------------------------------------------------------------------- the page


def test_the_page_lists_sources_grouped_by_topic(client) -> None:
    with session_scope(client.app.state.session_factory) as session:
        make_source(session, name="Lobsters", topic="tech")
        make_source(session, name="El Pais", topic="news")

    body = client.get("/sources").text

    assert "Lobsters" in body
    assert "El Pais" in body


def test_a_failing_source_is_flagged_as_dead(client) -> None:
    """A week of silence should look different from a flaky afternoon."""
    with session_scope(client.app.state.session_factory) as session:
        make_source(session, name="Gone", consecutive_failures=DEAD_AFTER)
        make_source(session, name="Flaky", consecutive_failures=1)

    body = client.get("/sources").text

    assert "Fallando" in body
    assert f"{DEAD_AFTER} seguidas" in body


# ----------------------------------------------------------------------------- toggling


def test_toggling_persists(client) -> None:
    with session_scope(client.app.state.session_factory) as session:
        source_id = make_source(session, enabled=True).id

    client.post("/sources/toggle", data={"source_id": source_id})

    with session_scope(client.app.state.session_factory) as session:
        assert session.get(Source, source_id).enabled is False


def test_toggling_back_on_works_too(client) -> None:
    with session_scope(client.app.state.session_factory) as session:
        source_id = make_source(session, enabled=False).id

    client.post("/sources/toggle", data={"source_id": source_id})

    with session_scope(client.app.state.session_factory) as session:
        assert session.get(Source, source_id).enabled is True


def test_a_disabled_source_is_skipped_by_the_next_poll(client) -> None:
    """Toggling is only meaningful if the poller honours it."""
    from feedfilter.fetcher import enabled_sources

    with session_scope(client.app.state.session_factory) as session:
        make_source(session, name="On")
        off = make_source(session, name="Off")

    client.post("/sources/toggle", data={"source_id": off.id})

    with session_scope(client.app.state.session_factory) as session:
        names = {source.name for source in enabled_sources(session)}
    assert "On" in names
    assert "Off" not in names


# ------------------------------------------------------------------------ adding by hand


def test_adding_a_feed(client) -> None:
    client.post("/sources/add", data={"url": "https://example.com/feed", "name": "Example"})

    with session_scope(client.app.state.session_factory) as session:
        source = session.scalars(
            select(Source).where(Source.url == "https://example.com/feed")
        ).one()
    assert source.name == "Example"


def test_adding_the_same_feed_twice_is_refused(client) -> None:
    for _ in range(2):
        client.post("/sources/add", data={"url": "https://example.com/feed", "name": "Example"})

    assert urls(client) == {"https://example.com/feed"}


def test_adding_something_that_is_not_a_url_is_refused(client) -> None:
    client.post("/sources/add", data={"url": "not a url", "name": "Nope"})

    assert urls(client) == set()


# -------------------------------------------------------------------------------- OPML


def test_opml_parsing_walks_nested_outlines() -> None:
    """Readers nest to whatever depth they like, and folders carry no xmlUrl."""
    feeds = parse_opml(OPML)

    assert [feed.url for feed in feeds] == [
        "https://lobste.rs/rss",
        "https://lwn.net/headlines/newrss",
        "https://example.com/feed",
    ]


def test_opml_prefers_title_and_falls_back_to_text() -> None:
    feeds = {feed.url: feed.name for feed in parse_opml(OPML)}

    assert feeds["https://lobste.rs/rss"] == "Lobsters"
    assert feeds["https://lwn.net/headlines/newrss"] == "LWN", "no title, so text"


def test_opml_deduplicates_by_url() -> None:
    doubled = OPML.replace(
        "</body>", '<outline type="rss" text="Again" xmlUrl="https://lobste.rs/rss"/></body>'
    )

    assert len(parse_opml(doubled)) == 3


@pytest.mark.parametrize(
    "data",
    [b"", b"not xml at all", b"<opml><body></body></opml>", b"<html><body>hi</body></html>"],
    ids=["empty", "not xml", "no feeds", "html"],
)
def test_unreadable_opml_is_refused(data) -> None:
    with pytest.raises(OpmlError):
        parse_opml(data)


def test_importing_adds_feeds_and_skips_known_ones(client) -> None:
    with session_scope(client.app.state.session_factory) as session:
        make_source(session, url="https://lobste.rs/rss", name="Already here")

    client.post("/sources/import", files={"file": ("subs.opml", OPML, "text/xml")})

    assert urls(client) == {
        "https://lobste.rs/rss",
        "https://lwn.net/headlines/newrss",
        "https://example.com/feed",
    }


def test_a_bad_upload_changes_nothing(client) -> None:
    client.post("/sources/import", files={"file": ("subs.opml", b"garbage", "text/xml")})

    assert urls(client) == set()
