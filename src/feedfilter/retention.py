"""Pruning old items.

A feed reader that never forgets turns into a database nobody can query. `FF_RETENTION_DAYS`
sets how far back the archive goes, counted from when an item was *fetched* rather than
when it claims to have been published -- feeds lie about publication dates, and an item
backdated by a year should not vanish the moment it arrives.

One exception, and it is the important part: an item carrying a label is never deleted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from .models import Item, Label, utcnow
from .settings import Settings


def cutoff_for(days: int, now: datetime | None = None) -> datetime:
    return (now or utcnow()) - timedelta(days=days)


def expired(cutoff: datetime):
    """Items old enough to prune, minus the ones that are training data.

    A labelled item is kept forever regardless of age. The 👍/👎 are the whole path to a
    classifier that actually discriminates, and they are worth nothing without the text
    they were a judgement about -- deleting the item would leave a label pointing at a
    row that no longer exists, and the database would erase it in the same breath through
    the foreign key.
    """
    return select(Item.id).where(
        Item.fetched_at < cutoff,
        ~exists().where(Label.item_id == Item.id),
    )


def prune_expired(
    session: Session, settings: Settings | None = None, *, now: datetime | None = None
) -> int:
    """Delete expired, unlabelled items. Returns how many rows went.

    Verdicts go with their item through the foreign key's ON DELETE CASCADE rather than
    through the ORM's relationship cascade, which a bulk delete does not consult. That is
    only true because db.py turns foreign key enforcement on; without it this would leave
    orphaned verdicts behind in silence.
    """
    cutoff = cutoff_for((settings or Settings()).retention_days, now)
    doomed = session.scalars(expired(cutoff)).all()
    if not doomed:
        return 0
    session.execute(delete(Item).where(Item.id.in_(doomed)))
    return len(doomed)
