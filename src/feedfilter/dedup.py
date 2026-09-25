"""Collapsing the same story republished across outlets.

Argentine and Spanish outlets run agency copy with minimal edits, so exact URL matching
catches almost none of it: two outlets covering one story share no URL at all. Comparing
headlines does catch it.

Both numbers below were measured against 815 real headlines from 21 AR and ES feeds
rather than chosen by taste. See the threshold's own note for what the data said.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Item, ItemAlias, utcnow
from .settings import Settings


@dataclass(frozen=True, slots=True)
class Match:
    item: Item
    score: float


def candidates(session: Session, *, source_id: int, since: datetime) -> list[Item]:
    """Items another source stored recently enough to still be the same news cycle.

    Same-source items are excluded, and that exclusion is not an optimisation -- it is
    what makes the whole thing safe. Outlets publish templated series: on one day Clarin
    carried "Dolar MEP hoy: a cuanto cotiza" and "Dolar cripto hoy: a cuanto cotiza",
    which score 96.6 against each other and are different articles. Perfil's "dolar blue"
    against its own "euro blue" scores 95.5. Across outlets the same pairs fall to 82,
    comfortably below the threshold, so dropping same-source comparisons removes the
    entire class of false positive. Republication by one outlet is the exact matcher's job
    anyway.
    """
    return list(
        session.scalars(select(Item).where(Item.source_id != source_id, Item.fetched_at >= since))
    )


def find_duplicate(
    session: Session,
    *,
    source_id: int,
    title: str,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> Match | None:
    """The best matching recent item from another source, or None.

    Returns the best match rather than the first, so a story carried by five outlets
    attaches to the one it actually resembles most.
    """
    settings = settings or Settings()
    if not title.strip():
        return None

    since = (now or utcnow()) - timedelta(hours=settings.dedup_window_hours)
    pool = candidates(session, source_id=source_id, since=since)
    if not pool:
        return None

    found = process.extractOne(
        title,
        [item.title for item in pool],
        scorer=fuzz.token_set_ratio,
        score_cutoff=settings.dedup_threshold,
    )
    if found is None:
        return None
    _matched_title, score, index = found
    return Match(item=pool[index], score=float(score))


def record_alias(
    session: Session, match: Match, *, source_id: int, url: str, title: str
) -> ItemAlias | None:
    """Note that another outlet carried this story. Returns None if already noted.

    Nothing is discarded: the other outlet's own headline and link are kept, along with
    the score that merged them, so a threshold that turns out to be wrong can be reviewed
    against what it actually did.
    """
    existing = session.scalars(
        select(ItemAlias).where(
            ItemAlias.item_id == match.item.id, ItemAlias.source_id == source_id
        )
    ).first()
    if existing is not None:
        return None

    alias = ItemAlias(
        item_id=match.item.id,
        source_id=source_id,
        url=url,
        title=title,
        score=match.score,
    )
    session.add(alias)
    session.flush()
    return alias
