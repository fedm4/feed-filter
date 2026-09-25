"""Running ingestion on a schedule, and on demand.

One cycle fetches every enabled source, stores what is new, and prunes what has expired.
It runs every ``FF_POLL_MINUTES`` and can also be triggered by hand from
``POST /admin/poll``.

Both paths share one lock, so "do not start a second overlapping run" holds whichever
way a run began. That matters more than it sounds: two concurrent cycles would fetch the
same feeds twice, and race each other inserting the same items.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from .classify import classify_pending, pending
from .db import session_scope
from .fetcher import FetchOutcome, poll_all
from .models import utcnow
from .retention import prune_expired
from .settings import Settings

log = logging.getLogger(__name__)

POLL_JOB_ID = "poll"


@dataclass(frozen=True, slots=True)
class PollReport:
    started_at: datetime
    seconds: float
    sources: int = 0
    stored: int = 0
    merged: int = 0
    failed: int = 0
    pruned: int = 0
    classified: int = 0
    unclassified: int = 0
    #: Still waiting after this cycle's limit was reached.
    backlog: int = 0
    outcomes: list[FetchOutcome] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"{self.sources} sources in {self.seconds:.1f}s: "
            f"{self.stored} new, {self.merged} merged, {self.failed} failed, "
            f"{self.classified} classified, {self.pruned} pruned"
            + (f", {self.backlog} still waiting" if self.backlog else "")
        )


def run_cycle(
    session: Session, settings: Settings | None = None, *, client: object | None = None
) -> PollReport:
    """One cycle: fetch everything, classify what is new, then prune.

    Classification comes after fetching so this cycle's items are judged in this cycle
    rather than waiting half an hour. Pruning goes last so an item fetched moments ago is
    never a candidate, and so neither a failed fetch nor a model that is down stops
    retention from running.
    """
    settings = settings or Settings()
    started = utcnow()
    clock = datetime.now().timestamp()

    outcomes = poll_all(session)
    verdicts = classify_pending(
        session,
        settings=settings,
        limit=settings.classify_max_per_cycle,
        # Injected by tests; in production the pipeline builds its own.
        client=client,
    )
    pruned = prune_expired(session, settings)

    report = PollReport(
        started_at=started,
        seconds=datetime.now().timestamp() - clock,
        sources=len(outcomes),
        stored=sum(outcome.stored for outcome in outcomes),
        merged=sum(outcome.merged for outcome in outcomes),
        failed=sum(1 for outcome in outcomes if not outcome.ok),
        pruned=pruned,
        classified=verdicts.classified,
        unclassified=verdicts.failed,
        backlog=len(pending(session)),
        outcomes=outcomes,
    )
    log.info("poll: %s", report.summary)
    return report


class PollRunner:
    """Runs a cycle, and refuses to run a second one at the same time.

    APScheduler's ``max_instances=1`` guards the scheduled path on its own, but a manual
    trigger arriving mid-cycle is a different caller and would slip past it. One lock
    covering both is the only version that actually holds.

    A refused run is not an error. Polling again a few seconds later has nothing to add,
    so the caller is told a cycle is already in progress and that is that.
    """

    def __init__(self, factory: sessionmaker[Session], settings: Settings | None = None) -> None:
        self._factory = factory
        self._settings = settings or Settings()
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def run(self) -> PollReport | None:
        """A report, or None when a cycle was already under way."""
        if not self._lock.acquire(blocking=False):
            log.info("poll: already running, skipping this trigger")
            return None
        try:
            with session_scope(self._factory) as session:
                return run_cycle(session, self._settings)
        finally:
            self._lock.release()


def create_scheduler(runner: PollRunner, settings: Settings | None = None) -> BackgroundScheduler:
    """A scheduler with the poll job registered. Not started.

    ``coalesce`` collapses runs missed while the process was down into one, rather than
    firing a burst of catch-up cycles at other people's servers on boot.
    """
    settings = settings or Settings()
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        runner.run,
        trigger=IntervalTrigger(minutes=settings.poll_minutes, timezone=settings.timezone),
        id=POLL_JOB_ID,
        name="Poll every enabled source",
        max_instances=1,
        coalesce=True,
        # Nothing is lost by being late, and a missed window is better than two cycles
        # racing. An hour of grace covers a laptop that was asleep.
        misfire_grace_time=3600,
    )
    return scheduler
