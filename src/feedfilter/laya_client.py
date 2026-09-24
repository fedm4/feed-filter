"""HTTP client for a Laya decision server.

Transport only. This module knows how to ask ``POST /v1/systemone`` and how to turn the
answer into typed objects; what to ask and what to conclude belong upstream.

Every answer keeps its **full probability distribution**, not just the winning label.
Thresholds are expected to move as the feedback labels accumulate, and moving one should
re-read stored distributions rather than re-run the classifier over the archive.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from .settings import Settings

_ENDPOINT = "/v1/systemone"

# Server-side caps, for reference when composing a request. They are not re-checked here:
# the server answers 413 and that surfaces as LayaRejected, and a second copy of a limit
# is a copy that goes stale.
MAX_QUESTIONS = 64
MAX_STATE_CHARS = 50_000


class LayaError(Exception):
    """Base for every failure this client reports."""


class LayaUnavailable(LayaError):
    """The server did not give a usable answer, and asking again might work.

    Connection failures, timeouts, 5xx, and bodies that do not parse. A truncated or
    garbled 200 lands here rather than in its own class because the caller's decision is
    the same one: this attempt is lost, the request itself was not refused.
    """


class LayaRejected(LayaError):
    """The server refused the request, and repeating it will not help.

    Any 4xx: a malformed body (400), too many questions or too much text (413), a question
    the model cannot parse (422), a bad API key (401).
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class Answer:
    """One question's answer, with the distribution it was drawn from.

    ``kind`` is Laya's question type:

    ``choice``  ``label`` holds the winning criterion, ``probabilities`` maps every
                criterion to its share.
    ``score``   ``score`` holds the expected value over the ladder, ``probabilities`` maps
                each rung (as ``"0"``, ``"1"``, ...) to its share and ``legend`` names them.
    ``noul``    a boolean. ``probabilities`` is ``{"true": p, "false": 1 - p}``, derived
                here: the wire format sends only the single float, and a caller reading
                distributions should not have to special-case one type of question.

    ``confidence`` comes from the model and is **not calibrated** on the base checkpoints;
    Laya itself warns that some shipped temperatures are out of range. Treat it as an
    ordering, not a probability.
    """

    kind: str
    probabilities: dict[str, float]
    confidence: float
    answer_confidence: float
    label: str | None = None
    score: float | None = None
    legend: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Decision:
    """Every answer from one call, plus which checkpoint produced it."""

    answers: dict[str, Answer]
    checkpoint: str | None = None
    routing_reason: str | None = None

    def __getitem__(self, question_id: str) -> Answer:
        return self.answers[question_id]


def _as_float(value: Any, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise LayaUnavailable(f"{where}: expected a number, got {value!r}") from exc


def _probabilities(raw: Mapping[str, Any], where: str) -> dict[str, float]:
    payload = raw.get("probabilities")
    if not isinstance(payload, Mapping) or not payload:
        raise LayaUnavailable(f"{where}: no probability distribution in the answer")
    return {str(k): _as_float(v, where) for k, v in payload.items()}


def _parse_answer(question_id: str, raw: Any) -> Answer:
    where = f"answer for {question_id!r}"
    if not isinstance(raw, Mapping):
        raise LayaUnavailable(f"{where}: expected an object, got {type(raw).__name__}")

    kind = raw.get("type")
    confidence = _as_float(raw.get("confidence"), where)
    answer_confidence = _as_float(raw.get("answer_confidence"), where)

    if kind == "choice":
        label = raw.get("choice")
        if not isinstance(label, str):
            raise LayaUnavailable(f"{where}: choice answer without a 'choice' label")
        return Answer(
            kind="choice",
            probabilities=_probabilities(raw, where),
            confidence=confidence,
            answer_confidence=answer_confidence,
            label=label,
        )

    if kind == "score":
        legend = raw.get("legend")
        legend = legend if isinstance(legend, Mapping) else {}
        return Answer(
            kind="score",
            probabilities=_probabilities(raw, where),
            confidence=confidence,
            answer_confidence=answer_confidence,
            score=_as_float(raw.get("score"), where),
            legend={str(k): str(v) for k, v in legend.items()},
        )

    if kind == "noul":
        true_p = _as_float(raw.get("noul"), where)
        return Answer(
            kind="noul",
            probabilities={"true": true_p, "false": round(1.0 - true_p, 4)},
            confidence=confidence,
            answer_confidence=answer_confidence,
        )

    raise LayaUnavailable(f"{where}: unknown question type {kind!r}")


def _parse_decision(payload: Any) -> Decision:
    if not isinstance(payload, Mapping):
        raise LayaUnavailable(f"response was {type(payload).__name__}, expected an object")
    answers = payload.get("answers")
    if not isinstance(answers, Mapping) or not answers:
        raise LayaUnavailable("response carried no answers")

    routing = payload.get("routing")
    routing = routing if isinstance(routing, Mapping) else {}
    return Decision(
        answers={str(qid): _parse_answer(str(qid), raw) for qid, raw in answers.items()},
        checkpoint=routing.get("model"),
        routing_reason=routing.get("reason"),
    )


def build_client(settings: Settings) -> httpx.Client:
    """The transport a ``LayaClient`` uses when it is not given one.

    Separate so that the wiring settings drive -- base URL, timeout, bearer token -- can be
    asserted on its own, without a live server and without reaching into a private
    attribute.
    """
    headers = {}
    if settings.laya_api_key:
        headers["authorization"] = f"Bearer {settings.laya_api_key}"
    return httpx.Client(
        base_url=settings.laya_base_url.rstrip("/"),
        timeout=settings.laya_timeout_s,
        headers=headers,
    )


class LayaClient:
    """Speaks to one Laya server.

    ``model`` is deliberately left out of the request unless ``FF_LAYA_MODEL`` is set. The
    server auto-routes by detected language when the field is absent, and auto-routing
    measured better than pinning: the sober English cable scored 0.60 auto against 1.03
    pinned to the multilingual checkpoint. Setting the variable turns that off.
    """

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self._model = settings.laya_model or None
        self._owns_client = client is None
        self._client = client or build_client(settings)

    def predict(self, state: Mapping[str, Any] | str, questions: Mapping[str, Any]) -> Decision:
        """Ask one set of questions about one item."""
        body: dict[str, Any] = {"state": state, "questions": dict(questions)}
        if self._model:
            body["model"] = self._model

        try:
            response = self._client.post(_ENDPOINT, json=body)
        except httpx.HTTPError as exc:
            raise LayaUnavailable(f"could not reach Laya: {exc}") from exc

        if response.is_client_error:
            raise LayaRejected(
                f"Laya refused the request ({response.status_code}): {_detail(response)}",
                status_code=response.status_code,
            )
        if not response.is_success:
            raise LayaUnavailable(f"Laya answered {response.status_code}: {_detail(response)}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise LayaUnavailable(
                f"Laya answered {response.status_code} with a non-JSON body"
            ) from exc
        return _parse_decision(payload)

    def predict_many(
        self, states: Iterable[Mapping[str, Any] | str], questions: Mapping[str, Any]
    ) -> list[Decision]:
        """The same questions over several items, in order.

        A plain loop, because there is nothing to win by doing otherwise. Laya exposes no
        batch endpoint, and its handler holds a single lock around inference, so requests
        fired concurrently queue on the server instead of overlapping. Concurrency here
        would buy contention and a harder failure story, not throughput.

        The first failure propagates: partial results are the caller's problem to define,
        and every caller so far wants all or nothing.
        """
        return [self.predict(state, questions) for state in states]

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LayaClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _detail(response: httpx.Response) -> str:
    """FastAPI's error shape is ``{"detail": ...}``; fall back to raw text."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, Mapping) and "detail" in payload:
        return str(payload["detail"])
    return response.text[:200]
