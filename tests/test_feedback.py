"""Label capture. Insert-only, and the count is what the page shows as progress."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import make_item, make_source
from feedfilter.db import session_scope
from feedfilter.main import create_app
from feedfilter.models import Label
from feedfilter.view import LABEL_TARGET


@pytest.fixture
def client(app_env):
    app = create_app()
    with TestClient(app) as started:
        started.app = app
        yield started


@pytest.fixture
def item_id(client):
    with session_scope(client.app.state.session_factory) as session:
        source = make_source(session)
        return make_item(session, source, title="A headline").id


@pytest.mark.parametrize("dimension", ["relevance", "sensationalism", "content_type"])
@pytest.mark.parametrize("verdict", ["agree", "disagree"])
def test_a_click_inserts_a_label(client, item_id, dimension, verdict) -> None:
    response = client.post(
        f"/items/{item_id}/label", data={"dimension": dimension, "verdict": verdict}
    )

    assert response.status_code == 200
    assert response.json()["labels"] == 1
    with session_scope(client.app.state.session_factory) as session:
        label = session.scalars(select(Label)).one()
    assert (label.dimension, label.value) == (dimension, verdict)


def test_relabelling_inserts_a_second_row(client, item_id) -> None:
    """Never an update: disagreeing with your earlier self is signal, not a correction."""
    client.post(f"/items/{item_id}/label", data={"dimension": "relevance", "verdict": "agree"})
    client.post(f"/items/{item_id}/label", data={"dimension": "relevance", "verdict": "disagree"})

    with session_scope(client.app.state.session_factory) as session:
        labels = session.scalars(select(Label).order_by(Label.id)).all()

    assert [label.value for label in labels] == ["agree", "disagree"]


def test_the_response_carries_the_running_count(client, item_id) -> None:
    for dimension in ("relevance", "sensationalism", "content_type"):
        body = client.post(
            f"/items/{item_id}/label", data={"dimension": dimension, "verdict": "agree"}
        ).json()

    assert body["labels"] == 3
    assert body["target"] == LABEL_TARGET


def test_an_unknown_dimension_is_refused(client, item_id) -> None:
    response = client.post(
        f"/items/{item_id}/label", data={"dimension": "vibes", "verdict": "agree"}
    )

    assert response.status_code == 422
    with session_scope(client.app.state.session_factory) as session:
        assert session.scalars(select(Label)).all() == []


def test_an_unknown_verdict_is_refused(client, item_id) -> None:
    response = client.post(
        f"/items/{item_id}/label", data={"dimension": "relevance", "verdict": "maybe"}
    )

    assert response.status_code == 422


def test_labelling_a_missing_item_is_a_404(client) -> None:
    response = client.post("/items/9999/label", data={"dimension": "relevance", "verdict": "agree"})

    assert response.status_code == 404


def test_the_feed_shows_progress_toward_fine_tuning(client, item_id) -> None:
    """So the work has a visible end rather than feeling unbounded."""
    client.post(f"/items/{item_id}/label", data={"dimension": "relevance", "verdict": "agree"})

    body = client.get("/").text

    assert str(LABEL_TARGET) in body
    assert "etiquetas para poder entrenar" in body
