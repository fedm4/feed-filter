"""Transport cases for the Laya client.

No network: every case drives an ``httpx.MockTransport``. The happy-path fixture is a
response captured from a real Laya 0.3.20 server rather than written by hand, so the
shapes here are the ones the client will actually meet -- including a ``noul`` answer,
which carries no ``probabilities`` key at all.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from feedfilter.laya_client import (
    LayaClient,
    LayaRejected,
    LayaUnavailable,
    build_client,
)
from feedfilter.settings import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "laya_response.json"

QUESTIONS = {
    "kind": {
        "type": "choice",
        "instructions": "What kind of piece is this?",
        "criteria": {"news": "a factual report of events", "opinion": "argues a position"},
    }
}
STATE = {"body": "The central bank raised rates by 25 basis points."}


@pytest.fixture
def response_payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def make_client(handler, **settings_kwargs: Any) -> LayaClient:
    """A client whose every request is answered by ``handler``."""
    settings = Settings(**settings_kwargs)
    transport = httpx.MockTransport(handler)
    return LayaClient(settings, client=httpx.Client(transport=transport, base_url="http://laya"))


def always(payload: Any, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return handler


def test_parses_every_answer_type(response_payload: dict[str, Any]) -> None:
    with make_client(always(response_payload)) as client:
        decision = client.predict(STATE, QUESTIONS)

    assert set(decision.answers) == {"kind", "sensationalism", "economics"}
    assert decision.checkpoint == "english"
    assert decision.routing_reason == "English Latin text"

    kind = decision["kind"]
    assert kind.kind == "choice"
    assert kind.label == "news"
    # The whole distribution survives, not just the winner: thresholds move later.
    assert set(kind.probabilities) == {"news", "analysis", "opinion", "promotion"}
    assert sum(kind.probabilities.values()) == pytest.approx(1.0, abs=1e-3)

    score = decision["sensationalism"]
    assert score.kind == "score"
    assert score.score == pytest.approx(response_payload["answers"]["sensationalism"]["score"])
    assert score.legend["0"] == "factual reporting"
    assert set(score.probabilities) == {"0", "1", "2", "3"}


def test_noul_distribution_is_derived(response_payload: dict[str, Any]) -> None:
    """A boolean answer ships one float; the client presents it as a distribution."""
    true_p = response_payload["answers"]["economics"]["noul"]

    with make_client(always(response_payload)) as client:
        answer = client.predict(STATE, QUESTIONS)["economics"]

    assert answer.kind == "noul"
    assert answer.probabilities == {"true": true_p, "false": pytest.approx(1 - true_p)}


def test_timeout_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with make_client(handler) as client, pytest.raises(LayaUnavailable, match="could not reach"):
        client.predict(STATE, QUESTIONS)


def test_connect_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with make_client(handler) as client, pytest.raises(LayaUnavailable):
        client.predict(STATE, QUESTIONS)


def test_server_error_is_unavailable() -> None:
    handler = always({"detail": "inference failed"}, status_code=500)
    with make_client(handler) as client, pytest.raises(LayaUnavailable, match="500"):
        client.predict(STATE, QUESTIONS)


def test_client_error_is_rejected_and_carries_the_status() -> None:
    # The real 422 body, as the server answers an unknown question type.
    detail = "question 'economics': unknown type 'boolean'; use one of ['choice', 'noul', 'score']"
    with make_client(always({"detail": detail}, status_code=422)) as client:
        with pytest.raises(LayaRejected) as caught:
            client.predict(STATE, QUESTIONS)

    assert caught.value.status_code == 422
    assert "unknown type" in str(caught.value)


def test_rejected_is_not_unavailable() -> None:
    """The split is what the retry logic in E3 will branch on, so pin it down."""
    with make_client(always({"detail": "nope"}, status_code=401)) as client:
        with pytest.raises(LayaRejected) as caught:
            client.predict(STATE, QUESTIONS)
    assert not isinstance(caught.value, LayaUnavailable)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"answers": {}},
        {"answers": {"kind": "not an object"}},
        {"answers": {"kind": {"type": "choice", "confidence": 0.5, "answer_confidence": 0.5}}},
        {"answers": {"kind": {"type": "telepathy", "confidence": 0.5, "answer_confidence": 0.5}}},
        [1, 2, 3],
    ],
    ids=[
        "no answers key",
        "empty answers",
        "answer not an object",
        "choice without label",
        "unknown type",
        "not an object",
    ],
)
def test_malformed_body_is_unavailable(payload: Any) -> None:
    with make_client(always(payload)) as client, pytest.raises(LayaUnavailable):
        client.predict(STATE, QUESTIONS)


def test_non_json_body_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    with make_client(handler) as client, pytest.raises(LayaUnavailable, match="non-JSON"):
        client.predict(STATE, QUESTIONS)


def test_model_is_omitted_so_the_server_auto_routes(response_payload: dict[str, Any]) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response_payload)

    with make_client(handler) as client:
        client.predict(STATE, QUESTIONS)

    assert "model" not in seen[0]
    assert seen[0]["state"] == STATE
    assert seen[0]["questions"] == QUESTIONS


def test_model_is_sent_when_pinned(response_payload: dict[str, Any]) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response_payload)

    with make_client(handler, laya_model="convaiinnovations/laya-multilingual") as client:
        client.predict(STATE, QUESTIONS)

    assert seen[0]["model"] == "convaiinnovations/laya-multilingual"


def test_predict_many_keeps_order_and_asks_once_per_state(
    response_payload: dict[str, Any],
) -> None:
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body["state"])
        payload = json.loads(json.dumps(response_payload))
        payload["routing"]["reason"] = body["state"]["body"]
        return httpx.Response(200, json=payload)

    states = [{"body": f"item {i}"} for i in range(3)]
    with make_client(handler) as client:
        decisions = client.predict_many(states, QUESTIONS)

    assert bodies == states
    assert [d.routing_reason for d in decisions] == ["item 0", "item 1", "item 2"]


def test_predict_many_propagates_the_first_failure(response_payload: dict[str, Any]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            return httpx.Response(500, json={"detail": "inference failed"})
        return httpx.Response(200, json=response_payload)

    with make_client(handler) as client, pytest.raises(LayaUnavailable):
        client.predict_many([STATE, STATE, STATE], QUESTIONS)

    assert calls == 2, "should stop at the failure rather than finish the batch"


def test_build_client_wires_settings() -> None:
    settings = Settings(
        laya_base_url="http://host.docker.internal:8000/",
        laya_api_key="s3cret",
        laya_timeout_s=12,
    )
    with build_client(settings) as client:
        assert str(client.base_url) == "http://host.docker.internal:8000"
        assert client.headers["authorization"] == "Bearer s3cret"
        assert client.timeout.read == 12


def test_build_client_sends_no_authorization_without_a_key() -> None:
    with build_client(Settings()) as client:
        assert "authorization" not in client.headers
