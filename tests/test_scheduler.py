"""The job is registered, and two cycles never overlap.

The overlap guard is the part worth testing hard. APScheduler's own ``max_instances``
covers the scheduled path, but a manual trigger arriving mid-cycle is a different caller
and would slip past it, so both paths have to meet the same lock.
"""

import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import make_source
from feedfilter.db import create_db_engine, create_session_factory, init_schema, session_scope
from feedfilter.main import create_app
from feedfilter.models import Item, Source
from feedfilter.scheduler import POLL_JOB_ID, PollRunner, create_scheduler, run_cycle
from feedfilter.settings import Settings

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Example</title>
  <item><title>An article</title><link>https://example.com/a</link></item>
</channel></rss>
"""


@pytest.fixture
def settings() -> Settings:
    return Settings(poll_minutes=5, tz="Europe/Madrid")


@pytest.fixture
def file_factory(tmp_path):
    """A session factory over a database on disk, usable from more than one thread.

    An in-memory SQLite gives each thread its *own* empty database, so a background
    thread sees no tables at all. The overlap test needs two threads looking at the same
    data, which is also the arrangement production runs: one file, WAL, and the
    busy_timeout set in db.py.
    """
    engine = create_db_engine(path=tmp_path / "ff.db")
    init_schema(engine)
    return create_session_factory(engine)


def stub_poll(monkeypatch, *, body=RSS, on_call=None):
    """Point the fetcher's client at a mock transport instead of the network."""

    def handler(request: httpx.Request) -> httpx.Response:
        if on_call is not None:
            on_call()
        return httpx.Response(200, content=body.encode())

    monkeypatch.setattr(
        "feedfilter.fetcher.build_client",
        lambda *args, **kwargs: httpx.Client(transport=httpx.MockTransport(handler)),
    )


# ---------------------------------------------------------------------------- the cycle


def test_a_cycle_fetches_and_prunes(factory, settings, monkeypatch) -> None:
    stub_poll(monkeypatch)

    with session_scope(factory) as session:
        make_source(session, name="Example")
        report = run_cycle(session, settings)

    assert report.sources == 1
    assert report.stored == 1
    assert report.failed == 0
    with session_scope(factory) as session:
        assert len(session.scalars(select(Item)).all()) == 1


def test_a_cycle_reports_failures_without_raising(factory, settings, monkeypatch) -> None:
    """A dead feed is an ordinary Tuesday; the cycle still has to finish and report."""
    monkeypatch.setattr(
        "feedfilter.fetcher.build_client",
        lambda *a, **k: httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ),
    )

    with session_scope(factory) as session:
        make_source(session)
        report = run_cycle(session, settings)

    assert report.failed == 1
    assert report.stored == 0


def test_the_summary_reads_as_one_line(factory, settings, monkeypatch) -> None:
    stub_poll(monkeypatch)
    with session_scope(factory) as session:
        make_source(session)
        report = run_cycle(session, settings)

    assert "1 new" in report.summary
    assert "0 failed" in report.summary


# ------------------------------------------------------------------------- registration


def test_the_poll_job_is_registered_with_the_configured_interval(factory, settings) -> None:
    scheduler = create_scheduler(PollRunner(factory, settings), settings)

    job = scheduler.get_job(POLL_JOB_ID)

    assert job is not None
    assert job.trigger.interval.total_seconds() == 5 * 60
    assert job.max_instances == 1, "APScheduler must not start a second scheduled run"
    assert job.coalesce is True, "missed windows collapse rather than firing a burst"


def test_the_scheduler_uses_the_configured_timezone(factory) -> None:
    settings = Settings(tz="America/Argentina/Buenos_Aires")

    scheduler = create_scheduler(PollRunner(factory, settings), settings)

    assert str(scheduler.timezone) == "America/Argentina/Buenos_Aires"


def test_the_scheduler_is_not_started_by_building_it(factory, settings) -> None:
    """Building it must be free of side effects; the lifespan decides when it runs."""
    assert create_scheduler(PollRunner(factory, settings), settings).running is False


# --------------------------------------------------------------------------- the guard


def test_a_second_run_is_refused_while_one_is_in_flight(
    file_factory, settings, monkeypatch
) -> None:
    """The acceptance criterion: a slow poll does not start a second overlapping run."""
    inside = threading.Event()
    release = threading.Event()
    refused: list[object] = []

    def block() -> None:
        inside.set()
        release.wait(timeout=5)

    stub_poll(monkeypatch, on_call=block)
    runner = PollRunner(file_factory, settings)
    with session_scope(file_factory) as session:
        make_source(session)

    slow = threading.Thread(target=runner.run)
    slow.start()
    try:
        assert inside.wait(timeout=5), "the first run never reached the fetch"
        assert runner.running is True
        refused.append(runner.run())
    finally:
        release.set()
        slow.join(timeout=5)

    assert refused == [None], "the overlapping trigger must be refused, not queued"
    assert runner.running is False, "the lock is released once the cycle ends"


def test_the_runner_is_reusable_after_a_refusal(factory, settings, monkeypatch) -> None:
    stub_poll(monkeypatch)
    runner = PollRunner(factory, settings)
    with session_scope(factory) as session:
        make_source(session)

    assert runner.run() is not None
    assert runner.run() is not None, "a refusal must not leave the lock held"


def test_a_failing_cycle_releases_the_lock(factory, settings, monkeypatch) -> None:
    """Otherwise one exception would wedge polling until the process restarted."""

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("feedfilter.scheduler.poll_all", explode)
    runner = PollRunner(factory, settings)

    with pytest.raises(RuntimeError):
        runner.run()

    assert runner.running is False


# ------------------------------------------------------------------------------ the app


def test_boot_registers_the_job_and_stops_it_on_shutdown(app_env) -> None:
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        scheduler = app.state.scheduler
        assert scheduler.running is True
        assert scheduler.get_job(POLL_JOB_ID) is not None

    assert scheduler.running is False, "the lifespan must stop the scheduler"


def test_admin_poll_ingests_immediately(app_env, monkeypatch) -> None:
    stub_poll(monkeypatch)

    app = create_app()
    with TestClient(app) as client:
        response = client.post("/admin/poll")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["sources"] == 1, "the source came from the catalogue synced on boot"
    assert body["stored"] == 1


def test_admin_poll_answers_409_while_a_cycle_is_running(app_env) -> None:
    app = create_app()
    with TestClient(app) as client:
        # Hold the runner's lock as a concurrent cycle would.
        assert app.state.poll_runner._lock.acquire(blocking=False)
        try:
            response = client.post("/admin/poll")
        finally:
            app.state.poll_runner._lock.release()

    assert response.status_code == 409
    assert response.json()["status"] == "already_running"


def test_boot_applies_the_catalogue(app_env) -> None:
    """Without this the sources table stays empty and a poll fetches nothing, quietly."""
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        with session_scope(app.state.session_factory) as session:
            names = [source.name for source in session.scalars(select(Source))]

    assert names == ["Seeded"]
