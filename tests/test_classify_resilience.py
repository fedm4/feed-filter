"""What classification does while Laya is unreachable.

Laya runs natively on the host, outside this app's lifecycle, so it is restarted,
upgraded and occasionally just down. None of that may cost an item.
"""

import logging

import httpx
import pytest
from sqlalchemy import select

from conftest import make_item, make_source
from feedfilter.classify import classify_items, classify_pending, pending, predict_with_retry
from feedfilter.config_file import DEFAULT_PATH, load_config
from feedfilter.db import session_scope
from feedfilter.laya_client import LayaClient, LayaRejected, LayaUnavailable
from feedfilter.models import Verdict
from feedfilter.questions import build_questions
from feedfilter.settings import Settings
from test_classify import decision

CONFIG = load_config(DEFAULT_PATH)
QUESTIONS = build_questions(CONFIG)


class Flaky:
    """Fails the first ``failures`` calls, then answers normally."""

    def __init__(self, failures: int, *, error: Exception | None = None) -> None:
        self.failures = failures
        self.error = error or LayaUnavailable("connection refused")
        self.calls = 0

    def predict(self, state, questions):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return decision()

    def close(self) -> None:
        pass


@pytest.fixture
def slept(monkeypatch):
    """Record what the backoff would have waited instead of waiting it."""
    delays: list[float] = []
    monkeypatch.setattr("feedfilter.classify.time.sleep", delays.append)
    return delays


# ------------------------------------------------------------------------------- backoff


def test_a_blip_is_ridden_out(slept) -> None:
    """Two refused connections then an answer: the item is classified, not lost."""
    client = Flaky(failures=2)
    result = predict_with_retry(client, _item(), QUESTIONS, retries=3, backoff_s=1.0)

    assert result.checkpoint == "english"
    assert client.calls == 3


def test_the_wait_doubles(slept) -> None:
    client = Flaky(failures=2)
    predict_with_retry(client, _item(), QUESTIONS, retries=3, backoff_s=0.5)

    assert slept == [0.5, 1.0]


def test_retries_are_capped(slept) -> None:
    """A model that never comes back must not retry forever."""
    client = Flaky(failures=99)
    with pytest.raises(LayaUnavailable):
        predict_with_retry(client, _item(), QUESTIONS, retries=3, backoff_s=1.0)

    assert client.calls == 3
    assert slept == [1.0, 2.0]


def test_a_rejected_request_is_not_retried(slept) -> None:
    """A 4xx is our fault. Sending the same request again more slowly does not fix it."""
    client = Flaky(failures=99, error=LayaRejected("bad question", status_code=422))
    with pytest.raises(LayaRejected):
        predict_with_retry(client, _item(), QUESTIONS, retries=3, backoff_s=1.0)

    assert client.calls == 1
    assert slept == []


def test_a_timeout_is_transient(slept) -> None:
    """Whatever httpx raises, the client has already turned it into LayaUnavailable."""
    transport = httpx.MockTransport(_timeout)
    with LayaClient(Settings(), client=httpx.Client(transport=transport)) as client:
        with pytest.raises(LayaUnavailable):
            predict_with_retry(client, _item(), QUESTIONS, retries=2, backoff_s=1.0)

    assert slept == [1.0]


# ------------------------------------------------------------------------- the whole run


def test_a_run_against_a_dead_model_stops_after_one_item(factory, slept, caplog) -> None:
    """Not one warning and one round of retries per item in the queue."""
    client = Flaky(failures=99)
    with session_scope(factory) as session:
        source = make_source(session)
        for n in range(20):
            make_item(session, source, title=f"Item {n}")

        with caplog.at_level(logging.WARNING):
            report = classify_items(
                session, pending(session), client=client, questions=QUESTIONS, retries=3
            )

    assert client.calls == 3, "three attempts at the first item, then it gave up"
    assert report.aborted
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_an_aborted_run_leaves_everything_pending(factory, slept) -> None:
    """A delay, never data loss: no verdicts means the next cycle picks them all up."""
    with session_scope(factory) as session:
        source = make_source(session)
        for n in range(5):
            make_item(session, source, title=f"Item {n}")

        classify_pending(session, config=CONFIG, client=Flaky(failures=99))

    with session_scope(factory) as session:
        assert session.scalars(select(Verdict)).all() == []
        assert len(pending(session)) == 5


def test_the_backlog_drains_once_the_model_is_back(factory, slept) -> None:
    """The recovery is the next cycle. Nothing has to be reset by hand."""
    client = Flaky(failures=99)
    with session_scope(factory) as session:
        source = make_source(session)
        for n in range(3):
            make_item(session, source, title=f"Item {n}")
        classify_pending(session, config=CONFIG, client=client)

    client.failures = 0
    with session_scope(factory) as session:
        report = classify_pending(session, config=CONFIG, client=client)

    assert (report.classified, report.aborted) == (3, False)

    with session_scope(factory) as session:
        assert pending(session) == []


def test_one_bad_item_does_not_stop_a_working_model(factory, slept) -> None:
    """Once something has gone through, Laya is up and the failure belongs to the item."""

    class OneBadItem(Flaky):
        def predict(self, state, questions):
            if state["title"] == "Poison":
                raise LayaUnavailable("could not parse the answer")
            return decision()

    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, title="Poison")
        make_item(session, source, title="Fine")  # newer, so it is classified first

        report = classify_items(
            session, pending(session), client=OneBadItem(0), questions=QUESTIONS, retries=2
        )

    assert (report.classified, report.failed, report.aborted) == (1, 1, False)


# ------------------------------------------------------------------------------- helpers


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("took too long", request=request)


def _item():
    """An unsaved item is enough: predict_with_retry only reads title and body."""

    class Stub:
        id = 1
        title = "Headline"
        body = None
        summary = None

    return Stub()
