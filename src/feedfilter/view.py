"""Shaping database rows into what a template needs.

Kept out of the route so the assembling can be tested without rendering HTML, and out of
the template so no query hides inside a loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .i18n import Translator
from .models import Item, ItemAlias, Verdict, utcnow
from .questions import LADDERS, ladder_score

#: Roughly the number of labels a useful fine-tune needs. Shown as a progress figure so
#: the work has a visible end rather than feeling unbounded.
LABEL_TARGET = 500


@dataclass(slots=True)
class ItemView:
    item: Item
    labels: dict[str, str] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    labelled: set[str] = field(default_factory=set)

    @property
    def classified(self) -> bool:
        return bool(self.labels)

    @property
    def content_type(self) -> str | None:
        return self.labels.get("content_type")

    def age(self, translator: Translator, now: datetime | None = None) -> str:
        seconds = (
            (now or utcnow()) - (self.item.published_at or self.item.fetched_at)
        ).total_seconds()
        minutes = int(seconds // 60)
        if minutes < 1:
            return translator("time.just_now")
        if minutes < 60:
            return translator("time.minutes", count=minutes)
        if minutes < 60 * 24:
            return translator("time.hours", count=minutes // 60)
        return translator("time.days", count=minutes // (60 * 24))


def latest_verdicts(session: Session, item_ids: list[int]) -> dict[int, dict[str, Verdict]]:
    """Newest verdict per question per item.

    Verdicts are insert-only, so an item reclassified twice has two rows per question and
    the newest wins. Sorting ascending and letting later rows overwrite earlier ones is
    the cheapest way to say that.
    """
    if not item_ids:
        return {}
    rows = session.scalars(
        select(Verdict).where(Verdict.item_id.in_(item_ids)).order_by(Verdict.created_at)
    )
    newest: dict[int, dict[str, Verdict]] = {}
    for verdict in rows:
        newest.setdefault(verdict.item_id, {})[verdict.question] = verdict
    return newest


def labelled_dimensions(session: Session, item_ids: list[int]) -> dict[int, set[str]]:
    """Which dimensions the user has already judged, per item."""
    if not item_ids:
        return {}
    from .models import Label

    out: dict[int, set[str]] = {}
    for label in session.scalars(select(Label).where(Label.item_id.in_(item_ids))):
        out.setdefault(label.item_id, set()).add(label.dimension)
    return out


def feed_items(session: Session, limit: int = 200) -> list[ItemView]:
    """The newest items, with their verdicts, alternate sources and labels attached."""
    items = list(
        session.scalars(
            select(Item)
            .options(
                selectinload(Item.source), selectinload(Item.aliases).selectinload(ItemAlias.source)
            )
            .order_by(Item.fetched_at.desc(), Item.id.desc())
            .limit(limit)
        )
    )
    ids = [item.id for item in items]
    verdicts = latest_verdicts(session, ids)
    labels = labelled_dimensions(session, ids)

    views = []
    for item in items:
        found = verdicts.get(item.id, {})
        view = ItemView(
            item=item,
            labels={question: verdict.top_label for question, verdict in found.items()},
            scores={
                question: ladder_score(question, verdict.distribution)
                for question, verdict in found.items()
                if question in LADDERS
            },
            aliases=[alias.source.name for alias in item.aliases],
            labelled=labels.get(item.id, set()),
        )
        views.append(view)
    return views


def label_count(session: Session) -> int:
    from sqlalchemy import func

    from .models import Label

    return session.scalar(select(func.count()).select_from(Label)) or 0
