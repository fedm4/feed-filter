"""Retention prunes what has expired, and nothing else.

The one rule worth more than the rest: a labelled item is training data and survives
whatever its age.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from feedfilter.db import create_db_engine, create_session_factory, init_schema, session_scope
from feedfilter.models import Item, Label, Source, Verdict, utcnow
from feedfilter.retention import prune_expired
from feedfilter.settings import Settings


@pytest.fixture
def factory():
    engine = create_db_engine(path=":memory:")
    init_schema(engine)
    return create_session_factory(engine)


@pytest.fixture
def settings() -> Settings:
    return Settings(retention_days=30)


def add_item(session, source: Source, *, age_days: float, url_hash: str) -> Item:
    item = Item(
        source_id=source.id,
        url=f"https://example.com/{url_hash}",
        canonical_url=f"https://example.com/{url_hash}",
        url_hash=url_hash,
        title=f"Item {url_hash}",
        fetched_at=utcnow() - timedelta(days=age_days),
    )
    session.add(item)
    session.flush()
    return item


def add_source(session) -> Source:
    source = Source(name="Example", url="https://example.com/feed", lang="en")
    session.add(source)
    session.flush()
    return source


def test_prunes_only_what_is_past_the_cutoff(factory, settings) -> None:
    with session_scope(factory) as session:
        source = add_source(session)
        add_item(session, source, age_days=31, url_hash="old")
        add_item(session, source, age_days=29, url_hash="recent")
        add_item(session, source, age_days=0, url_hash="fresh")

    with session_scope(factory) as session:
        removed = prune_expired(session, settings)

    assert removed == 1
    with session_scope(factory) as session:
        survivors = {item.url_hash for item in session.scalars(select(Item))}
    assert survivors == {"recent", "fresh"}


def test_a_labelled_item_is_never_pruned(factory, settings) -> None:
    """The 👍/👎 are the path to a classifier that discriminates; the text is half of one."""
    with session_scope(factory) as session:
        source = add_source(session)
        ancient = add_item(session, source, age_days=400, url_hash="ancient-labelled")
        add_item(session, source, age_days=400, url_hash="ancient-unlabelled")
        session.add(Label(item_id=ancient.id, dimension="relevance", value="essential"))

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
        source = add_source(session)
        old = add_item(session, source, age_days=90, url_hash="old")
        session.add(
            Verdict(item_id=old.id, question="kind", distribution={"news": 1.0}, top_label="news")
        )

    with session_scope(factory) as session:
        prune_expired(session, settings)

    with session_scope(factory) as session:
        assert session.scalars(select(Verdict)).all() == []


def test_the_source_survives_its_items(factory, settings) -> None:
    with session_scope(factory) as session:
        source = add_source(session)
        add_item(session, source, age_days=90, url_hash="old")

    with session_scope(factory) as session:
        prune_expired(session, settings)

    with session_scope(factory) as session:
        assert len(session.scalars(select(Source)).all()) == 1


def test_nothing_to_prune_is_not_an_error(factory, settings) -> None:
    with session_scope(factory) as session:
        source = add_source(session)
        add_item(session, source, age_days=1, url_hash="fresh")

    with session_scope(factory) as session:
        assert prune_expired(session, settings) == 0


def test_retention_days_is_respected(factory) -> None:
    with session_scope(factory) as session:
        source = add_source(session)
        add_item(session, source, age_days=10, url_hash="ten-days-old")

    with session_scope(factory) as session:
        assert prune_expired(session, Settings(retention_days=30)) == 0
    with session_scope(factory) as session:
        assert prune_expired(session, Settings(retention_days=7)) == 1


def test_now_can_be_pinned(factory, settings) -> None:
    """The caller supplies the clock, so a test never races midnight."""
    with session_scope(factory) as session:
        source = add_source(session)
        add_item(session, source, age_days=10, url_hash="ten-days-old")

    with session_scope(factory) as session:
        # Pretend it is 40 days later: the item is now well past a 30 day window.
        removed = prune_expired(session, settings, now=utcnow() + timedelta(days=40))

    assert removed == 1
