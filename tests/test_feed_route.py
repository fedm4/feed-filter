"""The feed page renders items, and the opinion rules hold."""

import pytest
from fastapi.testclient import TestClient

from conftest import make_item, make_source, make_verdict
from feedfilter.db import session_scope
from feedfilter.main import create_app
from feedfilter.view import feed_items


def classify(
    session, item, *, content_type="news", relevance="interesting", sensationalism="factual"
):
    """Four verdicts, as the pipeline would write them."""
    make_verdict(
        session,
        item,
        question="content_type",
        top_label=content_type,
        distribution={content_type: 0.9, "news": 0.1},
    )
    make_verdict(
        session,
        item,
        question="relevance",
        top_label=relevance,
        distribution={"irrelevant": 0.0, "marginal": 0.1, "interesting": 0.7, "essential": 0.2},
    )
    make_verdict(
        session,
        item,
        question="sensationalism",
        top_label=sensationalism,
        distribution={"factual": 0.8, "slanted": 0.15, "clickbait": 0.05, "tabloid": 0.0},
    )
    make_verdict(
        session, item, question="topic", top_label="technology", distribution={"technology": 1.0}
    )


@pytest.fixture
def client(app_env):
    """A started app. The schema is created by the lifespan, so seeding happens in here."""
    app = create_app()
    with TestClient(app) as started:
        started.app = app
        yield started


@pytest.fixture
def seeded(client):
    with session_scope(client.app.state.session_factory) as session:
        source = make_source(session, name="Example News")
        classified = make_item(
            session, source, title="A classified headline", summary="Some summary."
        )
        classify(session, classified)
        make_item(session, source, title="Not yet judged")
    return client


def test_the_feed_lists_items(seeded) -> None:
    body = seeded.get("/").text

    assert "A classified headline" in body
    assert "Not yet judged" in body
    assert "Example News" in body


def test_the_feed_shows_relevance_and_sensationalism(seeded) -> None:
    body = seeded.get("/").text

    assert "Interesante" in body, "relevance label, translated"
    assert "Sobrio" in body, "sensationalism label, translated"


def test_scores_ride_along_as_data_attributes(seeded) -> None:
    """The sliders filter on these, so no round trip and no reclassification."""
    body = seeded.get("/").text

    assert 'data-relevance="2.100"' in body
    assert 'data-sensationalism="0.250"' in body


def test_an_unclassified_item_is_marked_as_such(seeded) -> None:
    body = seeded.get("/").text

    assert "Sin clasificar" in body
    assert 'data-classified="no"' in body


def test_an_empty_feed_says_so(client) -> None:
    assert "Todavía no hay nada" in client.get("/").text


def test_the_page_renders_in_english_with_no_code_change(app_env, monkeypatch) -> None:
    monkeypatch.setenv("FF_UI_LANG", "en")
    with TestClient(create_app()) as client:
        with session_scope(client.app.state.session_factory) as session:
            source = make_source(session)
            classify(session, make_item(session, source, title="A headline"))
        body = client.get("/").text

    assert "Sources" in body and "Fuentes" not in body
    assert "Interesting" in body


# ------------------------------------------------------------------- opinion is never lost


@pytest.mark.parametrize("policy", ["tag", "collapse", "hide"])
def test_opinion_is_always_badged_whatever_the_policy(app_env, monkeypatch, policy) -> None:
    """The rule: opinion and promotion are never dropped silently. The badge is not
    conditional; the policy only decides whether the item is also folded or hidden."""
    monkeypatch.setenv("FF_OPINION_POLICY", policy)
    with TestClient(create_app()) as client:
        with session_scope(client.app.state.session_factory) as session:
            source = make_source(session)
            classify(
                session,
                make_item(session, source, title="An argued position"),
                content_type="opinion",
            )
        body = client.get("/").text

    assert "badge-opinion" in body
    assert "Opinión" in body
    assert "An argued position" in body, "still in the document, whatever the policy"
    assert f'"{policy}"' in body, "the policy reaches the page so the script can act on it"


def test_promotion_is_badged_too(client) -> None:
    with session_scope(client.app.state.session_factory) as session:
        source = make_source(session)
        classify(session, make_item(session, source, title="Buy this"), content_type="promotion")
    body = client.get("/").text

    assert "badge-promotion" in body
    assert "Promoción" in body


def test_thresholds_from_settings_become_slider_positions(app_env, monkeypatch) -> None:
    monkeypatch.setenv("FF_MIN_RELEVANCE", "interesting")
    monkeypatch.setenv("FF_MAX_SENSATIONALISM", "slanted")
    with TestClient(create_app()) as client:
        body = client.get("/").text

    assert 'id="min-relevance"' in body
    assert 'value="2"' in body, "interesting is the third rung"
    assert 'value="1"' in body, "slanted is the second"


# ------------------------------------------------------------------------------ the view


def test_the_newest_verdict_wins(client) -> None:
    """Verdicts are insert-only, so reclassifying leaves two rows for one question."""
    from datetime import timedelta

    from feedfilter.models import utcnow

    with session_scope(client.app.state.session_factory) as session:
        source = make_source(session)
        item = make_item(session, source)
        make_verdict(
            session,
            item,
            question="content_type",
            top_label="news",
            distribution={"news": 1.0},
            created_at=utcnow() - timedelta(days=1),
        )
        make_verdict(
            session,
            item,
            question="content_type",
            top_label="opinion",
            distribution={"opinion": 1.0},
        )

        views = feed_items(session)

    assert views[0].content_type == "opinion"


def test_alternate_sources_are_listed(client) -> None:
    from feedfilter.models import ItemAlias

    with session_scope(client.app.state.session_factory) as session:
        first = make_source(session, name="El Pais")
        item = make_item(session, first, title="One story")
        other = make_source(session, name="El Mundo")
        session.add(
            ItemAlias(
                item_id=item.id,
                source_id=other.id,
                url="https://e/a",
                title="One story, other words",
                score=95.0,
            )
        )

    body = client.get("/").text

    assert "El Mundo" in body
    assert "También en" in body
