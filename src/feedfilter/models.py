"""The four tables.

Two kinds of table live here, and the difference governs how they are written.

``Source`` and ``Item`` describe the world as it currently is, and are updated in place:
a feed's ETag changes, an item gains a body once the full text is fetched.

``Verdict`` and ``Label`` are records of fact -- what the classifier said at a point in
time, what the user said at a point in time. They are insert-only and never updated.
Reclassifying an item inserts another verdict; the newest one wins. That keeps the
history needed to see whether a criteria change actually helped, and it is what makes the
labels usable as training data later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator


def utcnow() -> datetime:
    """Timezone-aware UTC. Storage is always UTC; ``FF_TZ`` is a display concern."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """A datetime that is still timezone-aware after a round trip.

    SQLite has no datetime type: values go in as text and come back as naive, so
    ``DateTime(timezone=True)`` silently drops the offset there. Left alone, that turns
    into a crash the first time retention compares a stored ``fetched_at`` against an
    aware ``now``, since Python refuses to order naive against aware.

    So the conversion is made explicit. Values are normalised to UTC on the way in and
    re-tagged as UTC on the way out. A naive value is rejected rather than assumed to be
    UTC: feeds publish times in whatever zone they please, and quietly guessing is how
    items end up hours out of place.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime reached the database; attach a timezone before storing"
            )
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


class Source(Base):
    """A feed being polled."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(2000), unique=True)

    # Curation axes. The catalogue in D1/D2 fills these, and the UI filters on them.
    lang: Mapped[str] = mapped_column(String(8))
    country: Mapped[str | None] = mapped_column(String(8), default=None)
    topic: Mapped[str | None] = mapped_column(String(64), default=None)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Conditional-request state, so a poll that finds nothing new costs one 304.
    etag: Mapped[str | None] = mapped_column(String(400), default=None)
    last_modified: Mapped[str | None] = mapped_column(String(100), default=None)

    # A feed that breaks stays in the table rather than disappearing: the count is what
    # lets the UI show "this one has been failing for a week" instead of silence.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    items: Mapped[list[Item]] = relationship(back_populates="source", cascade="all, delete-orphan")


class Item(Base):
    """One article from one feed."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))

    url: Mapped[str] = mapped_column(String(2000))
    # The URL with tracking parameters and other noise stripped (D4). Two feeds carrying
    # the same story rarely agree on the raw URL and usually agree on this one.
    canonical_url: Mapped[str] = mapped_column(String(2000))
    # Dedup key: a hash of canonical_url, not the URL itself, because SQLite indexes a
    # fixed-width column far more happily than a 2000-character one.
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)

    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    # Filled only when FF_FETCH_FULL_TEXT is on (D7); the feed's own summary is the
    # fallback and is usually what the classifier sees.
    body: Mapped[str | None] = mapped_column(Text, default=None)

    # What the feed claims, which is sometimes absent and sometimes a lie.
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # When we saw it, which is always true. Retention counts from here.
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    source: Mapped[Source] = relationship(back_populates="items")
    verdicts: Mapped[list[Verdict]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    labels: Mapped[list[Label]] = relationship(back_populates="item", cascade="all, delete-orphan")
    aliases: Mapped[list[ItemAlias]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_items_fetched_at", "fetched_at"),)


class Verdict(Base):
    """What the classifier answered for one question about one item.

    Insert-only. ``distribution`` keeps the **whole** probability distribution rather than
    the winning label alone, so a threshold can move later by re-reading stored rows
    instead of re-classifying the archive -- which, at the measured 4.67 items/sec, is the
    difference between instant and an evening.

    Its keys are whatever the classification layer stored. They should be stable English
    identifiers (``news``, ``opinion``, ...) and never the model's positional indices, or
    a row stops being readable the moment a criteria list is reordered.
    """

    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))

    question: Mapped[str] = mapped_column(String(64))
    distribution: Mapped[dict[str, Any]] = mapped_column(JSON)
    top_label: Mapped[str] = mapped_column(String(64))
    # Which checkpoint answered, as the server reports it. Kept because the router picks
    # per item, so a mixed-language feed produces rows from more than one.
    model: Mapped[str | None] = mapped_column(String(100), default=None)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    item: Mapped[Item] = relationship(back_populates="verdicts")

    # "The newest verdict for this question on this item" is the only read that matters,
    # and it runs for every item on every page render.
    __table_args__ = (
        Index("ix_verdicts_item_question_created", "item_id", "question", "created_at"),
    )


class Label(Base):
    """The user's own judgement on one item: the 👍/👎 the feed collects.

    Insert-only, like ``Verdict``. Changing your mind inserts a second row rather than
    overwriting the first, because a disagreement with your earlier self is exactly the
    kind of signal fine-tuning wants to see rather than lose.

    One label per dimension per item at a time is the intent, but it is not a database
    constraint: the uniqueness is over the *newest* row, which no constraint can express.
    """

    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))

    # Which axis is being judged: "relevance", "sensationalism", "kind".
    dimension: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    item: Mapped[Item] = relationship(back_populates="labels")

    __table_args__ = (
        UniqueConstraint("item_id", "dimension", "created_at", name="uq_label_item_dimension_time"),
    )


class ItemAlias(Base):
    """Another outlet that carried the same story.

    When fuzzy matching decides two headlines are the same agency cable, the first one
    stays as the Item and the rest land here instead of being thrown away. The UI reads
    this to say "also in: Clarin, Infobae", which is more useful than either showing the
    story five times or silently hiding four of them.

    Keeping the other outlet's own headline matters: the wording differences are exactly
    what a reader might want to see, and what a later review of a wrong merge needs.
    """

    __tablename__ = "item_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))

    url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str] = mapped_column(Text)
    # The score that merged them, kept so a bad threshold can be reviewed after the fact
    # rather than argued about from memory.
    score: Mapped[float] = mapped_column(Float)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    item: Mapped[Item] = relationship(back_populates="aliases")
    source: Mapped[Source] = relationship()

    # One outlet appears once per story. A second near-identical piece from the same
    # outlet is its own article, not another alias of this one.
    __table_args__ = (UniqueConstraint("item_id", "source_id", name="uq_alias_item_source"),)
