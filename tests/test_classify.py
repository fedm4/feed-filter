"""Classification: what gets picked up, what gets stored, and what survives a failure."""

import pytest
from sqlalchemy import select

from conftest import make_item, make_source, make_verdict
from feedfilter.classify import ClassifyReport, classify_pending, item_state, pending
from feedfilter.config_file import DEFAULT_PATH, load_config
from feedfilter.db import session_scope
from feedfilter.laya_client import Answer, Decision, LayaError, LayaUnavailable
from feedfilter.models import Verdict
from feedfilter.questions import build_questions
from feedfilter.settings import Settings

CONFIG = load_config(DEFAULT_PATH)
QUESTIONS = build_questions(CONFIG)


def decision() -> Decision:
    """What Laya returns for the four questions: ladders keyed by position, choices by name."""
    return Decision(
        answers={
            "relevance": Answer(
                kind="score",
                probabilities={"0": 0.05, "1": 0.15, "2": 0.6, "3": 0.2},
                confidence=0.4,
                answer_confidence=0.6,
                score=1.95,
            ),
            "sensationalism": Answer(
                kind="score",
                probabilities={"0": 0.7, "1": 0.2, "2": 0.07, "3": 0.03},
                confidence=0.5,
                answer_confidence=0.7,
                score=0.43,
            ),
            "content_type": Answer(
                kind="choice",
                probabilities={"news": 0.8, "analysis": 0.1, "opinion": 0.07, "promotion": 0.03},
                confidence=0.6,
                answer_confidence=0.8,
                label="news",
            ),
            "topic": Answer(
                kind="choice",
                probabilities={"technology": 0.9, "economy": 0.1},
                confidence=0.7,
                answer_confidence=0.9,
                label="technology",
            ),
        },
        checkpoint="english",
    )


class FakeClient:
    """Stands in for LayaClient. Records what it was asked."""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_on = fail_on or set()

    def predict(self, state, questions):
        self.calls.append({"state": state, "questions": questions})
        if state["title"] in self.fail_on:
            raise LayaUnavailable("model is down")
        return decision()

    def close(self) -> None:
        pass


# ------------------------------------------------------------------------ what is pending


def test_only_items_without_verdicts_are_pending(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        done = make_item(session, source, title="Already judged")
        make_verdict(session, done)
        make_item(session, source, title="Waiting")

        assert [item.title for item in pending(session)] == ["Waiting"]


def test_pending_drains_newest_first(factory) -> None:
    """The feed is read newest-first, so that is the only useful order to classify in.

    Oldest-first looked fairer and was useless: with any backlog, the items on screen
    were exactly the unjudged ones, so the page showed no scores and no feedback buttons.
    """
    from datetime import timedelta

    from feedfilter.models import utcnow

    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, title="Old", fetched_at=utcnow() - timedelta(days=2))
        make_item(session, source, title="New", fetched_at=utcnow())

        assert [item.title for item in pending(session)] == ["New", "Old"]


# ---------------------------------------------------------------------------- what Laya sees


def test_the_body_is_preferred_over_the_summary(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        item = make_item(session, source, summary="Teaser...", body="The full article.")

        assert item_state(item)["body"] == "The full article."


def test_the_summary_is_used_when_there_is_no_body(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        item = make_item(session, source, summary="Teaser...", body=None)

        assert item_state(item)["body"] == "Teaser..."


def test_an_item_with_neither_still_gets_a_title(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        item = make_item(session, source, title="Just a headline", summary=None, body=None)

        assert item_state(item) == {"title": "Just a headline"}


# ------------------------------------------------------------------------------ storage


def test_every_item_gets_four_verdicts(factory) -> None:
    """The acceptance criterion."""
    client = FakeClient()
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, title="One")
        make_item(session, source, title="Two")

        report = classify_pending(session, config=CONFIG, client=client)

    assert report.classified == 2
    assert report.verdicts == 8
    with session_scope(factory) as session:
        questions = {v.question for v in session.scalars(select(Verdict))}
    assert questions == {"relevance", "sensationalism", "content_type", "topic"}


def test_a_ladder_distribution_is_stored_by_identifier(factory) -> None:
    """Positions would stop meaning anything the moment a ladder was reordered."""
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source)
        classify_pending(session, config=CONFIG, client=FakeClient())

    with session_scope(factory) as session:
        verdict = session.scalars(select(Verdict).where(Verdict.question == "relevance")).one()

    assert set(verdict.distribution) == {"irrelevant", "marginal", "interesting", "essential"}
    assert verdict.distribution["interesting"] == pytest.approx(0.6)
    assert verdict.top_label == "interesting"


def test_a_choice_distribution_passes_straight_through(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source)
        classify_pending(session, config=CONFIG, client=FakeClient())

    with session_scope(factory) as session:
        verdict = session.scalars(select(Verdict).where(Verdict.question == "content_type")).one()

    assert verdict.top_label == "news"
    assert verdict.distribution["news"] == pytest.approx(0.8)


def test_the_whole_distribution_is_kept_not_just_the_winner(factory) -> None:
    """Moving a threshold later has to be a read, not a reclassification."""
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source)
        classify_pending(session, config=CONFIG, client=FakeClient())

    with session_scope(factory) as session:
        for verdict in session.scalars(select(Verdict)):
            assert len(verdict.distribution) >= 2
            assert sum(verdict.distribution.values()) == pytest.approx(1.0, abs=0.02)


def test_the_checkpoint_that_answered_is_recorded(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source)
        classify_pending(session, config=CONFIG, client=FakeClient())

    with session_scope(factory) as session:
        assert session.scalars(select(Verdict)).first().model == "english"


# ------------------------------------------------------------------------------ failures


def test_a_failing_item_leaves_the_others_alone(factory) -> None:
    """A model that is briefly down should cost one item, not the run."""
    client = FakeClient(fail_on={"Broken"})
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, title="Broken")
        make_item(session, source, title="Fine")

        report = classify_pending(session, config=CONFIG, client=client)

    assert (report.classified, report.failed) == (1, 1)


def test_a_failed_item_stays_pending(factory) -> None:
    """Nothing is written for it, so the next cycle picks it up again."""
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source, title="Broken")
        classify_pending(session, config=CONFIG, client=FakeClient(fail_on={"Broken"}))

    with session_scope(factory) as session:
        assert [item.title for item in pending(session)] == ["Broken"]


def test_classifying_twice_does_not_duplicate_verdicts(factory) -> None:
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source)
        classify_pending(session, config=CONFIG, client=FakeClient())
    with session_scope(factory) as session:
        second = classify_pending(session, config=CONFIG, client=FakeClient())

    assert second.classified == 0
    with session_scope(factory) as session:
        assert len(session.scalars(select(Verdict)).all()) == 4


def test_nothing_pending_asks_the_model_nothing(factory) -> None:
    client = FakeClient()
    with session_scope(factory) as session:
        assert classify_pending(session, config=CONFIG, client=client).classified == 0

    assert client.calls == []


def test_the_four_questions_are_the_ones_sent(factory) -> None:
    client = FakeClient()
    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source)
        classify_pending(session, config=CONFIG, client=client)

    assert set(client.calls[0]["questions"]) == set(QUESTIONS)


def test_a_limit_caps_one_run(factory) -> None:
    client = FakeClient()
    with session_scope(factory) as session:
        source = make_source(session)
        for index in range(5):
            make_item(session, source, title=f"Item {index}")

        report = classify_pending(session, config=CONFIG, client=client, limit=2)

    assert report.classified == 2
    assert len(client.calls) == 2


def test_laya_error_is_the_only_thing_caught(factory) -> None:
    """A bug in our own code must not be swallowed as if the model were down."""

    class Exploding(FakeClient):
        def predict(self, state, questions):
            raise ValueError("a bug, not a model failure")

    with session_scope(factory) as session:
        source = make_source(session)
        make_item(session, source)

        with pytest.raises(ValueError):
            classify_pending(session, config=CONFIG, client=Exploding())

    assert issubclass(LayaUnavailable, LayaError)


def test_the_cycle_passes_its_limit_through(factory, monkeypatch) -> None:
    """At ~1.3s an item, an unbounded first run would hold the poll lock for hours."""
    import feedfilter.scheduler as scheduler

    seen = {}

    def spy(session, **kwargs):
        seen.update(kwargs)
        return ClassifyReport()

    monkeypatch.setattr(scheduler, "classify_pending", spy)
    with session_scope(factory) as session:
        scheduler.run_cycle(session, Settings(classify_max_per_cycle=2))

    assert seen["limit"] == 2


def test_the_backlog_is_what_is_still_pending(factory) -> None:
    """So a first run over a full catalogue reports how far behind it still is."""
    from feedfilter.scheduler import run_cycle

    with session_scope(factory) as session:
        source = make_source(session, enabled=False)
        for index in range(5):
            make_item(session, source, title=f"Item {index}")

    with session_scope(factory) as session:
        report = run_cycle(session, Settings(classify_max_per_cycle=2), client=FakeClient())

    assert report.classified == 2
    assert report.backlog == 3, "the rest wait for the next cycle"


def test_a_capped_cycle_classifies_what_the_reader_will_see(factory) -> None:
    """The regression this order exists to prevent."""
    from datetime import timedelta

    from feedfilter.models import utcnow
    from feedfilter.view import feed_items

    with session_scope(factory) as session:
        source = make_source(session)
        for age in range(10):
            make_item(
                session, source, title=f"Item {age}", fetched_at=utcnow() - timedelta(hours=age)
            )
        classify_pending(session, config=CONFIG, client=FakeClient(), limit=3)

    with session_scope(factory) as session:
        top = feed_items(session)[:3]

    assert all(view.classified for view in top), "the top of the feed must carry verdicts"
