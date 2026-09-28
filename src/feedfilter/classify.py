"""Running stored items past Laya and keeping what it said.

One item at a time, because that is all the server can do: it has no batch endpoint and
holds a single lock around inference, so grouping requests only makes them queue. That was
measured in #7, and it is why FF_LAYA_BATCH_SIZE was removed rather than used here.

Every answer is stored as a Verdict keyed by stable identifier, never by the model's
positional index -- a row keyed by position stops being readable the moment a ladder is
reordered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from .config_file import Config, load_config
from .laya_client import Answer, Decision, LayaClient, LayaError
from .models import Item, Verdict
from .questions import LADDERS, build_questions, rung_label
from .settings import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClassifyReport:
    classified: int = 0
    failed: int = 0
    verdicts: int = 0

    @property
    def summary(self) -> str:
        return f"{self.classified} classified, {self.failed} failed, {self.verdicts} verdicts"


def pending(session: Session, limit: int | None = None) -> list[Item]:
    """Items with no verdict yet, newest first.

    Newest first because that is the order the feed is read in. Draining oldest first
    seemed fairer and was actually useless: with any backlog at all, the items on screen
    were precisely the ones still unjudged, so the page showed no scores and no feedback
    buttons while a thousand week-old items were classified out of sight.

    Old items left unclassified are not a loss. They are below the fold, and retention
    removes them before anyone scrolls that far.
    """
    statement = (
        select(Item)
        .where(~exists().where(Verdict.item_id == Item.id))
        .order_by(Item.fetched_at.desc(), Item.id.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def item_state(item: Item) -> dict[str, str]:
    """What Laya is shown. The body when there is one, the feed's summary otherwise."""
    state = {"title": item.title}
    text = item.body or item.summary
    if text:
        state["body"] = text
    return state


def distribution_for(question: str, answer: Answer) -> dict[str, float]:
    """The answer's distribution, keyed by identifier.

    A ladder comes back keyed by position ("0", "1", ...), which says nothing on its own.
    A choice already uses identifiers and passes straight through.
    """
    if question not in LADDERS:
        return dict(answer.probabilities)
    return {
        rung_label(question, position): value for position, value in answer.probabilities.items()
    }


def top_label_for(question: str, answer: Answer) -> str:
    if question in LADDERS:
        return rung_label(question, max(answer.probabilities, key=answer.probabilities.get))
    return answer.label or max(answer.probabilities, key=answer.probabilities.get)


def store_verdicts(session: Session, item: Item, decision: Decision) -> int:
    """One Verdict row per question. Returns how many were written."""
    for question, answer in decision.answers.items():
        session.add(
            Verdict(
                item_id=item.id,
                question=question,
                distribution=distribution_for(question, answer),
                top_label=top_label_for(question, answer),
                model=decision.checkpoint,
            )
        )
    session.flush()
    return len(decision.answers)


def classify_items(
    session: Session,
    items: list[Item],
    *,
    client: LayaClient,
    questions: dict[str, dict[str, Any]],
) -> ClassifyReport:
    """Classify each item and store its verdicts.

    A failure on one item is counted and the rest continue: the item simply stays pending
    and is picked up next cycle, which is the right answer for a model that is briefly
    down. E3 adds backoff on top of this.
    """
    classified = failed = verdicts = 0
    for item in items:
        try:
            decision = client.predict(item_state(item), questions)
        except LayaError as exc:
            failed += 1
            log.warning("classify: item %d failed (%s)", item.id, type(exc).__name__)
            continue
        verdicts += store_verdicts(session, item, decision)
        classified += 1
    return ClassifyReport(classified=classified, failed=failed, verdicts=verdicts)


def classify_pending(
    session: Session,
    *,
    settings: Settings | None = None,
    config: Config | None = None,
    client: LayaClient | None = None,
    limit: int | None = None,
) -> ClassifyReport:
    """Classify everything waiting. Returns a report, never raises on a model failure."""
    settings = settings or Settings()
    items = pending(session, limit)
    if not items:
        return ClassifyReport()

    questions = build_questions(config or load_config(settings=settings))
    owned = client is None
    client = client or LayaClient(settings)
    try:
        report = classify_items(session, items, client=client, questions=questions)
    finally:
        if owned:
            client.close()
    log.info("classify: %s", report.summary)
    return report
