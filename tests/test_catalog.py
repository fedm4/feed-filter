"""Loading and syncing the catalogue. No network: the URLs are strings here.

Reaching them is `scripts/verify_catalog.py`, which is a separate job on purpose -- a
test suite that fails because someone's blog is down is a test suite people learn to
ignore.
"""

import pytest
import yaml
from sqlalchemy import select

from conftest import make_source
from feedfilter.catalog import (
    EXAMPLE_PATH,
    CatalogError,
    load_catalog,
    sync_catalog,
)
from feedfilter.db import session_scope
from feedfilter.models import Source
from feedfilter.settings import Settings

ENTRY = {"name": "Example", "url": "https://example.com/feed", "lang": "en", "topic": "tech"}


def write_catalog(tmp_path, *entries):
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump({"sources": list(entries)}), encoding="utf-8")
    return path


def test_loads_every_field(tmp_path) -> None:
    path = write_catalog(tmp_path, {**ENTRY, "country": "es", "enabled": False})

    entry = load_catalog(path)[0]

    assert entry.name == "Example"
    assert entry.url == "https://example.com/feed"
    assert entry.lang == "en"
    assert entry.country == "es"
    assert entry.topic == "tech"
    assert entry.enabled is False


def test_optional_fields_default(tmp_path) -> None:
    entry = load_catalog(
        write_catalog(tmp_path, {"name": "N", "url": "https://n/f", "lang": "en"})
    )[0]

    assert entry.country is None
    assert entry.topic is None
    assert entry.enabled is True


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**ENTRY, "langauge": "en"}, "unknown field"),
        ({"url": "https://e/f", "lang": "en"}, "missing name"),
        ({"name": "E", "lang": "en"}, "missing url"),
        ({"name": "E", "url": "https://e/f"}, "missing lang"),
        ({"name": "E", "url": "", "lang": "en"}, "missing url"),
        ({"name": "E", "url": "example.com/feed", "lang": "en"}, "must be http"),
        ({"name": "E", "url": "ftp://example.com/feed", "lang": "en"}, "must be http"),
        ("just a string", "expected a mapping"),
    ],
    ids=[
        "typo in field name",
        "no name",
        "no url",
        "no lang",
        "empty url",
        "schemeless url",
        "wrong scheme",
        "not a mapping",
    ],
)
def test_malformed_entries_are_rejected(tmp_path, entry, message) -> None:
    """A silently ignored typo is a feed that never polls, which is hard to notice."""
    with pytest.raises(CatalogError, match=message):
        load_catalog(write_catalog(tmp_path, entry))


def test_a_repeated_url_is_rejected(tmp_path) -> None:
    """url is unique in the database, so a duplicate would fail far from its cause."""
    path = write_catalog(tmp_path, ENTRY, {**ENTRY, "name": "Same feed, other name"})

    with pytest.raises(CatalogError, match="repeats entry 1"):
        load_catalog(path)


@pytest.mark.parametrize(
    "document",
    [{}, {"feeds": []}, {"sources": []}, {"sources": "not a list"}, []],
    ids=["empty", "wrong key", "empty list", "not a list", "not a mapping"],
)
def test_malformed_documents_are_rejected(tmp_path, document) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(CatalogError):
        load_catalog(path)


def test_the_shipped_example_is_valid() -> None:
    """The file people copy. Parsed on every run: cheap, and it has one job."""
    entries = load_catalog(EXAMPLE_PATH)

    assert len(entries) >= 8
    assert all(entry.lang for entry in entries)
    assert len({entry.url for entry in entries}) == len(entries)


def test_sync_inserts_new_sources(factory) -> None:
    entries = load_catalog(EXAMPLE_PATH)

    with session_scope(factory) as session:
        report = sync_catalog(session, entries)

    assert report.added == len(entries)
    assert report.updated == 0
    with session_scope(factory) as session:
        assert len(session.scalars(select(Source)).all()) == len(entries)


def test_sync_is_idempotent(factory) -> None:
    entries = load_catalog(EXAMPLE_PATH)

    with session_scope(factory) as session:
        sync_catalog(session, entries)
    with session_scope(factory) as session:
        report = sync_catalog(session, entries)

    assert report.added == 0
    assert report.updated == 0
    assert report.unchanged == len(entries)


def test_sync_refreshes_descriptive_fields(tmp_path, factory) -> None:
    with session_scope(factory) as session:
        make_source(session, url="https://example.com/feed", name="Old name", topic="misc")

    entries = load_catalog(write_catalog(tmp_path, ENTRY))
    with session_scope(factory) as session:
        report = sync_catalog(session, entries)

    assert report.updated == 1
    with session_scope(factory) as session:
        source = session.scalars(select(Source)).one()
    assert source.name == "Example"
    assert source.topic == "tech"


def test_sync_never_re_enables_what_the_user_switched_off(tmp_path, factory) -> None:
    """Otherwise every boot would undo the decision, which is worse than not having a switch."""
    with session_scope(factory) as session:
        make_source(session, url="https://example.com/feed", enabled=False)

    entries = load_catalog(write_catalog(tmp_path, {**ENTRY, "enabled": True}))
    with session_scope(factory) as session:
        sync_catalog(session, entries)

    with session_scope(factory) as session:
        assert session.scalars(select(Source)).one().enabled is False


def test_sync_never_deletes_a_source_missing_from_the_file(tmp_path, factory) -> None:
    """Deleting it would take its items, verdicts and labels along through the cascade."""
    with session_scope(factory) as session:
        make_source(session, url="https://retired.example/feed", name="Retired")

    entries = load_catalog(write_catalog(tmp_path, ENTRY))
    with session_scope(factory) as session:
        sync_catalog(session, entries)

    with session_scope(factory) as session:
        urls = {source.url for source in session.scalars(select(Source))}
    assert "https://retired.example/feed" in urls


def test_sync_leaves_fetch_state_alone(tmp_path, factory) -> None:
    """ETags and failure counts belong to the poller, not to the file."""
    with session_scope(factory) as session:
        make_source(
            session,
            url="https://example.com/feed",
            name="Old name",
            etag='W/"abc"',
            consecutive_failures=3,
        )

    entries = load_catalog(write_catalog(tmp_path, ENTRY))
    with session_scope(factory) as session:
        sync_catalog(session, entries)

    with session_scope(factory) as session:
        source = session.scalars(select(Source)).one()
    assert source.etag == 'W/"abc"'
    assert source.consecutive_failures == 3


def test_a_missing_catalogue_says_how_to_make_one(tmp_path) -> None:
    """Silently falling back to the example would mean polling a list nobody chose."""
    missing = tmp_path / "data" / "catalog.yaml"

    with pytest.raises(CatalogError) as caught:
        load_catalog(settings=Settings(catalog_path=missing))

    message = str(caught.value)
    assert str(missing) in message
    assert "catalog.example.yaml" in message


def test_the_polled_catalogue_comes_from_settings(tmp_path) -> None:
    path = write_catalog(tmp_path, {**ENTRY, "name": "From settings"})

    entries = load_catalog(settings=Settings(catalog_path=path))

    assert entries[0].name == "From settings"
