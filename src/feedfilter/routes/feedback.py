"""Capturing the user's own judgement.

This exists in v1 because zero-shot relevance is the known weak spot: these labels are the
only path from a filter that guesses to one that has been trained. #19 measured the model
over 197 real items and the argument has not got weaker since.

Labels are insert-only. Changing your mind writes another row rather than overwriting the
first, because a disagreement with your earlier self is signal a fine-tune wants to see.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse

from ..db import session_scope
from ..models import Item, Label
from ..view import LABEL_TARGET, label_count

router = APIRouter(tags=["feedback"])

#: What can be judged. The model answers a fourth question, topic, but agreeing with a
#: topic says little about whether the filter is working -- these three are the axes worth
#: spending clicks on.
DIMENSIONS = ("relevance", "sensationalism", "content_type")
VERDICTS = ("agree", "disagree")


@router.post("/items/{item_id}/label")
def add_label(
    item_id: int,
    request: Request,
    dimension: str = Form(...),
    verdict: str = Form(...),
) -> JSONResponse:
    """Record agreement or disagreement with one dimension of one item."""
    if dimension not in DIMENSIONS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown dimension {dimension!r}"
        )
    if verdict not in VERDICTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown verdict {verdict!r}")

    with session_scope(request.app.state.session_factory) as session:
        if session.get(Item, item_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item")
        session.add(Label(item_id=item_id, dimension=dimension, value=verdict))
        session.flush()
        total = label_count(session)

    return JSONResponse({"status": "ok", "labels": total, "target": LABEL_TARGET})
