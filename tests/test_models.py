"""Round-trips for the four tables, on an in-memory SQLite.

The interesting cases are the ones SQLite gets wrong unless asked otherwise: foreign keys
are off by default, and datetimes come back without their timezone.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from feedfilter.db import create_db_engine, create_session_factory, session_scope
from feedfilter.models import Base, Item, Label, Source, Verdict, utcnow


@pytest.fixture
def factory():
    engine = create_db_engine(path=":memory:")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def make_source(session: Session, **overrides) -> Source:
    source = Source(
        **{"name": "Example", "url": "https://example.com/feed", "lang": "en", **overrides}
    )
    session.add(source)
    session.flush()
    return source


def make_item(session: Session, source: Source, **overrides) -> Item:
    item = Item(
        **{
            "source_id": source.id,
            "url": "https://example.com/a?utm_source=rss",
            "canonical_url": "https://example.com/a",
            "url_hash": "hash-a",
            "title": "A headline",
            **overrides,
        }
    )
    session.add(item)
    session.flush()
    return item


def test_source_round_trip(factory) -> None:
    with session_scope(factory) as session:
        make_source(session, country="es", topic="tech", etag='W/"abc"')

    with session_scope(factory) as session:
        source = session.scalars(select(Source)).one()

    assert source.name == "Example"
    assert source.country == "es"
    assert source.enabled is True
    assert source.consecutive_failures == 0
    assert source.last_modified is None


def test_item_round_trip_and_default_fetched_at(factory) -> None:
    before = utcnow()
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, summary="A summary")

    with session_scope(factory) as session:
        item = session.scalars(select(Item)).one()

    assert item.summary == "A summary"
    assert item.body is None
    assert item.published_at is None
    assert before <= item.fetched_at <= utcnow()


def test_datetimes_keep_their_timezone(factory) -> None:
    """SQLite stores no offset, so the type has to put it back or retention breaks."""
    published = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, published_at=published)

    with session_scope(factory) as session:
        item = session.scalars(select(Item)).one()

    assert item.published_at == published
    assert item.published_at.tzinfo is not None
    assert item.fetched_at.tzinfo is not None
    # The point of all of it: this comparison must not raise.
    assert item.fetched_at > utcnow() - timedelta(minutes=1)


def test_non_utc_input_is_stored_as_utc(factory) -> None:
    madrid_summer = datetime(2026, 7, 1, 14, 0, tzinfo=UTC).astimezone()
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, published_at=madrid_summer)

    with session_scope(factory) as session:
        item = session.scalars(select(Item)).one()

    assert item.published_at == madrid_summer
    assert item.published_at.tzinfo is UTC


def test_naive_datetime_is_refused(factory) -> None:
    """Guessing a feed's timezone silently is how items land hours out of place.

    SQLAlchemy wraps anything a bind parameter raises, so the caller sees a
    StatementError; the ValueError is underneath it.
    """
    with pytest.raises(StatementError, match="naive datetime") as caught:
        with session_scope(factory) as session:
            source = make_source(session)
            make_item(session, source, published_at=datetime(2026, 1, 2, 3, 4))

    assert isinstance(caught.value.orig, ValueError)


def test_url_hash_is_unique(factory) -> None:
    with pytest.raises(IntegrityError):
        with session_scope(factory) as session:
            source = make_source(session)
            make_item(session, source)
            make_item(
                session, source, url="https://example.com/b", canonical_url="https://example.com/b"
            )


def test_foreign_keys_are_enforced(factory) -> None:
    """SQLite leaves them off by default, which would make every CASCADE decorative."""
    with pytest.raises(IntegrityError):
        with session_scope(factory) as session:
            session.add(
                Item(
                    source_id=999,
                    url="u",
                    canonical_url="u",
                    url_hash="orphan",
                    title="orphan",
                )
            )


def test_deleting_a_source_takes_its_items_and_verdicts(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        item = make_item(session, source)
        session.add(
            Verdict(item_id=item.id, question="kind", distribution={"news": 1.0}, top_label="news")
        )

    with session_scope(factory) as session:
        session.delete(session.scalars(select(Source)).one())

    with session_scope(factory) as session:
        assert session.scalars(select(Item)).all() == []
        assert session.scalars(select(Verdict)).all() == []


def test_verdict_keeps_the_whole_distribution(factory) -> None:
    distribution = {"news": 0.6427, "analysis": 0.2, "opinion": 0.1073, "promotion": 0.05}

    with session_scope(factory) as session:
        source = make_source(session)
        item = make_item(session, source)
        session.add(
            Verdict(
                item_id=item.id,
                question="kind",
                distribution=distribution,
                top_label="news",
                model="english",
            )
        )

    with session_scope(factory) as session:
        verdict = session.scalars(select(Verdict)).one()

    # A dict, not a string: the JSON column has to survive the round trip as structure,
    # or every reader has to remember to parse it.
    assert verdict.distribution == distribution
    assert verdict.distribution["opinion"] == pytest.approx(0.1073)
    assert verdict.model == "english"


def test_verdicts_are_insert_only_and_the_newest_wins(factory) -> None:
    """Reclassifying adds a row; it does not overwrite what the model said before."""
    with session_scope(factory) as session:
        source = make_source(session)
        item = make_item(session, source)
        session.add(
            Verdict(
                item_id=item.id,
                question="kind",
                distribution={"opinion": 0.9},
                top_label="opinion",
                created_at=utcnow() - timedelta(days=1),
            )
        )
        session.add(
            Verdict(item_id=item.id, question="kind", distribution={"news": 0.8}, top_label="news")
        )

    with session_scope(factory) as session:
        rows = session.scalars(select(Verdict).order_by(Verdict.created_at.desc())).all()

    assert len(rows) == 2, "the older verdict must survive"
    assert rows[0].top_label == "news"


def test_label_round_trip(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        item = make_item(session, source)
        session.add(Label(item_id=item.id, dimension="relevance", value="essential"))

    with session_scope(factory) as session:
        label = session.scalars(select(Label)).one()

    assert label.dimension == "relevance"
    assert label.value == "essential"
    assert label.created_at.tzinfo is not None


def test_changing_your_mind_adds_a_label_rather_than_replacing_one(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        item = make_item(session, source)
        session.add(
            Label(
                item_id=item.id,
                dimension="relevance",
                value="marginal",
                created_at=utcnow() - timedelta(hours=1),
            )
        )
        session.add(Label(item_id=item.id, dimension="relevance", value="essential"))

    with session_scope(factory) as session:
        labels = session.scalars(select(Label).order_by(Label.created_at)).all()

    assert [label.value for label in labels] == ["marginal", "essential"]


def test_session_scope_rolls_back_on_failure(factory) -> None:
    with pytest.raises(RuntimeError):
        with session_scope(factory) as session:
            make_source(session)
            raise RuntimeError("boom")

    with session_scope(factory) as session:
        assert session.scalars(select(Source)).all() == []
