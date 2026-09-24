"""Retention prunes what has expired, and nothing else.

The one rule worth more than the rest: a labelled item is training data and survives
whatever its age.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from conftest import make_item, make_label, make_source, make_verdict
from feedfilter.db import session_scope
from feedfilter.models import Item, Label, Source, Verdict, utcnow
from feedfilter.retention import prune_expired
from feedfilter.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(retention_days=30)


def aged(days: float) -> dict:
    """Kwargs placing an item that far in the past. Retention counts from fetched_at."""
    return {"fetched_at": utcnow() - timedelta(days=days)}


def test_prunes_only_what_is_past_the_cutoff(factory, settings) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, url_hash="old", **aged(31))
        make_item(session, source, url_hash="recent", **aged(29))
        make_item(session, source, url_hash="fresh", **aged(0))

    with session_scope(factory) as session:
        removed = prune_expired(session, settings)

    assert removed == 1
    with session_scope(factory) as session:
        survivors = {item.url_hash for item in session.scalars(select(Item))}
    assert survivors == {"recent", "fresh"}


def test_a_labelled_item_is_never_pruned(factory, settings) -> None:
    """The 👍/👎 are the path to a classifier that discriminates; the text is half of one."""
    with session_scope(factory) as session:
        source = make_source(session)
        ancient = make_item(session, source, url_hash="ancient-labelled", **aged(400))
        make_item(session, source, url_hash="ancient-unlabelled", **aged(400))
        make_label(session, ancient)

    with session_scope(factory) as session:
        removed = prune_expired(session, settings)

    assert removed == 1
    with session_scope(factory) as session:
        survivors = {item.url_hash for item in session.scalars(select(Item))}
        assert survivors == {"ancient-labelled"}
        assert len(session.scalars(select(Label)).all()) == 1


def test_verdicts_go_with_their_item(factory, settings) -> None:
    """A bulk delete skips the ORM cascade, so this leans on the foreign key instead."""
    with session_scope(factory) as session:
        source = make_source(session)
        old = make_item(session, source, url_hash="old", **aged(90))
        make_verdict(session, old, distribution={"news": 1.0})

    with session_scope(factory) as session:
        prune_expired(session, settings)

    with session_scope(factory) as session:
        assert session.scalars(select(Verdict)).all() == []


def test_the_source_survives_its_items(factory, settings) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, url_hash="old", **aged(90))

    with session_scope(factory) as session:
        prune_expired(session, settings)

    with session_scope(factory) as session:
        assert len(session.scalars(select(Source)).all()) == 1


def test_nothing_to_prune_is_not_an_error(factory, settings) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, url_hash="fresh", **aged(1))

    with session_scope(factory) as session:
        assert prune_expired(session, settings) == 0


def test_retention_days_is_respected(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, url_hash="ten-days-old", **aged(10))

    with session_scope(factory) as session:
        assert prune_expired(session, Settings(retention_days=30)) == 0
    with session_scope(factory) as session:
        assert prune_expired(session, Settings(retention_days=7)) == 1


def test_now_can_be_pinned(factory, settings) -> None:
    """The caller supplies the clock, so a test never races midnight."""
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, url_hash="ten-days-old", **aged(10))

    with session_scope(factory) as session:
        # Pretend it is 40 days later: the item is now well past a 30 day window.
        removed = prune_expired(session, settings, now=utcnow() + timedelta(days=40))

    assert removed == 1
