"""Extraction from saved HTML. No network."""

import httpx
import pytest
from sqlalchemy import select

from conftest import make_source
from feedfilter.db import session_scope
from feedfilter.extract import MAX_BODY_CHARS, extract_body, fetch_body, truncate
from feedfilter.fetcher import poll_source
from feedfilter.models import Item
from feedfilter.settings import Settings

ARTICLE = """<!doctype html>
<html><head><title>Rates rise</title></head><body>
  <nav><a href="/">Home</a><a href="/world">World</a></nav>
  <article>
    <h1>Central bank raises rates by 25 basis points</h1>
    <p>The central bank raised its benchmark rate by 25 basis points on Thursday,
       citing core inflation that has stayed above target for six months.</p>
    <p>Policymakers signalled one further increase before the end of the year, and
       three of the nine members dissented.</p>
  </article>
  <footer>Subscribe to our newsletter for more</footer>
</body></html>
"""

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example</title>
  <item>
    <title>Central bank raises rates by 25 basis points</title>
    <link>https://example.com/rates</link>
    <description>The central bank raised its benchmark rate... [read more]</description>
  </item>
</channel></rss>
"""


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_extracts_the_article_and_drops_the_furniture() -> None:
    body = extract_body(ARTICLE)

    assert body is not None
    assert "core inflation" in body
    assert "three of the nine members dissented" in body
    assert "Subscribe to our newsletter" not in body, "footer is not the article"
    assert "Home" not in body, "nav is not the article"


def test_a_page_with_no_text_gives_nothing() -> None:
    """The caller then keeps the feed's summary. Note trafilatura is happy to return a
    navigation menu if that is all a page has, so None means genuinely empty."""
    assert extract_body("<html><body></body></html>") is None


def test_truncation_keeps_whole_sentences() -> None:
    text = ("Sentence one is here. " * 300).strip()

    cut = truncate(text)

    assert len(cut) <= MAX_BODY_CHARS
    assert cut.endswith("."), "a half sentence is worse for the model than a shorter one"


def test_short_text_is_left_alone() -> None:
    assert truncate("Just this.") == "Just this."


def test_fetch_returns_nothing_when_the_page_fails() -> None:
    """The feed's own summary stays as the fallback rather than the item being dropped."""
    with client_for(lambda request: httpx.Response(404)) as client:
        assert fetch_body(client, "https://example.com/gone") is None


def test_fetch_returns_nothing_on_a_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with client_for(handler) as client:
        assert fetch_body(client, "https://example.com/slow") is None


@pytest.mark.parametrize("flag", [False, True], ids=["flag off", "flag on"])
def test_the_flag_decides_whether_the_page_is_fetched(factory, monkeypatch, flag) -> None:
    """With the flag off there must be no extra request at all: one per article adds up."""
    monkeypatch.setenv("FF_FETCH_FULL_TEXT", "true" if flag else "false")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/rates":
            return httpx.Response(200, content=ARTICLE.encode())
        return httpx.Response(200, content=RSS.encode())

    with session_scope(factory) as session:
        source = make_source(session, url="https://example.com/feed")
        poll_source(session, client_for(handler), source)

    with session_scope(factory) as session:
        item = session.scalars(select(Item)).one()

    if flag:
        assert "https://example.com/rates" in requested
        assert item.body is not None
        assert "core inflation" in item.body
    else:
        assert requested == ["https://example.com/feed"]
        assert item.body is None
    assert item.summary is not None, "the feed's summary is kept either way"


def test_the_default_is_off() -> None:
    assert Settings().fetch_full_text is False
