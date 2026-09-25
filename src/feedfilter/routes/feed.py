"""The feed itself."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..db import session_scope
from ..settings import Relevance, Sensationalism, Settings
from ..templating import render
from ..view import LABEL_TARGET, feed_items, label_count

router = APIRouter(tags=["feed"])

#: The ladders as slider positions. FF_MIN_RELEVANCE and FF_MAX_SENSATIONALISM name a rung;
#: the slider needs its index.
RELEVANCE_STEPS = [step.value for step in Relevance]
SENSATIONALISM_STEPS = [step.value for step in Sensationalism]


def threshold_positions(settings: Settings) -> tuple[int, int]:
    return (
        RELEVANCE_STEPS.index(settings.min_relevance.value),
        SENSATIONALISM_STEPS.index(settings.max_sensationalism.value),
    )


@router.get("/")
def feed(request: Request):
    settings: Settings = request.app.state.settings
    minimum, maximum = threshold_positions(settings)

    with session_scope(request.app.state.session_factory) as session:
        views = feed_items(session)
        labels = label_count(session)

    return render(
        request.app.state.templates,
        request,
        "feed.html",
        page="feed",
        views=views,
        min_relevance=minimum,
        max_sensationalism=maximum,
        hide_promotion=settings.hide_promotion,
        opinion_policy=settings.opinion_policy.value,
        label_count=labels,
        label_target=LABEL_TARGET,
    )
