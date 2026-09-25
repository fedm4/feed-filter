"""A table of dirty URLs and what they should reduce to.

The bias throughout is to strip only what is provably not part of the address. A wrong
merge loses an article in silence; a missed merge shows a duplicate, which is visible and
harmless by comparison. Where the tests below look conservative, that is why.
"""

import pytest

from feedfilter.canonical import canonicalize, url_hash

CASES = [
    # --- nothing to do -------------------------------------------------------------
    ("https://example.com/article", "https://example.com/article"),
    ("https://example.com/", "https://example.com/"),
    ("https://example.com", "https://example.com/"),
    # --- tracking parameters -------------------------------------------------------
    (
        "https://example.com/a?utm_source=rss&utm_medium=feed&utm_campaign=x",
        "https://example.com/a",
    ),
    ("https://example.com/a?fbclid=IwAR0abc", "https://example.com/a"),
    ("https://example.com/a?gclid=xyz&ref=newsletter", "https://example.com/a"),
    (
        "https://www.bbc.co.uk/news/x?at_medium=RSS&at_campaign=KARANGA",
        "https://www.bbc.co.uk/news/x",
    ),
    ("https://www.nytimes.com/2026/01/a.html?smid=rss", "https://www.nytimes.com/2026/01/a.html"),
    ("https://example.com/a?UTM_SOURCE=RSS", "https://example.com/a"),
    # --- real parameters survive ----------------------------------------------------
    ("https://example.com/search?q=rust", "https://example.com/search?q=rust"),
    ("https://example.com/a?id=42&utm_source=rss", "https://example.com/a?id=42"),
    (
        "https://www.lanacion.com.ar/feed/?outputType=xml&utm_source=rss",
        "https://www.lanacion.com.ar/feed?outputType=xml",
    ),
    # --- order does not matter ------------------------------------------------------
    ("https://example.com/a?b=2&a=1", "https://example.com/a?a=1&b=2"),
    ("https://example.com/a?a=1&b=2", "https://example.com/a?a=1&b=2"),
    # --- case and default ports -----------------------------------------------------
    ("HTTPS://Example.COM/Article", "https://example.com/Article"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("https://example.com:8443/a", "https://example.com:8443/a"),
    # --- fragments ------------------------------------------------------------------
    ("https://example.com/a#section-2", "https://example.com/a"),
    ("https://example.com/a?utm_source=x#top", "https://example.com/a"),
    # --- trailing slash -------------------------------------------------------------
    ("https://example.com/a/", "https://example.com/a"),
    ("https://example.com/a/b/", "https://example.com/a/b"),
]


@pytest.mark.parametrize(("dirty", "clean"), CASES, ids=[c[0][:58] for c in CASES])
def test_canonical_form(dirty: str, clean: str) -> None:
    assert canonicalize(dirty) == clean


def test_canonicalizing_twice_changes_nothing() -> None:
    """It has to be idempotent, or a stored key stops matching a recomputed one."""
    for dirty, _ in CASES:
        once = canonicalize(dirty)
        assert canonicalize(once) == once


def test_the_path_is_left_alone_apart_from_a_trailing_slash() -> None:
    """Case in a path is significant on most servers; changing it invents a new URL."""
    assert (
        canonicalize("https://example.com/Path/To/Article") == "https://example.com/Path/To/Article"
    )


def test_www_is_not_stripped() -> None:
    """Plenty of sites serve different things with and without it. Guessing merges two
    articles into one, and a wrong merge is the expensive mistake here."""
    assert canonicalize("https://www.example.com/a") == "https://www.example.com/a"
    assert canonicalize("https://example.com/a") != canonicalize("https://www.example.com/a")


def test_http_and_https_stay_apart() -> None:
    """Upgrading the scheme would be a guess about a server we have not asked."""
    assert canonicalize("http://example.com/a") != canonicalize("https://example.com/a")


@pytest.mark.parametrize("value", ["", "   ", "not a url", "/relative/path", "mailto:a@b.com"])
def test_things_that_are_not_absolute_urls_are_handed_back(value: str) -> None:
    """Nothing here can be normalised safely, and inventing a scheme would be a guess."""
    assert canonicalize(value) == value.strip()


def test_two_tracking_variants_hash_the_same() -> None:
    """This is the whole point: one article, two feeds, one row."""
    from_twitter = "https://example.com/story?utm_source=twitter&utm_medium=social"
    from_rss = "https://example.com/story?utm_source=rss#lede"

    assert url_hash(from_twitter) == url_hash(from_rss)


def test_different_articles_do_not_collide() -> None:
    assert url_hash("https://example.com/a") != url_hash("https://example.com/b")


def test_the_hash_is_a_sha256_hex_digest() -> None:
    """The column is String(64); a longer digest would be silently truncated."""
    digest = url_hash("https://example.com/a")

    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
