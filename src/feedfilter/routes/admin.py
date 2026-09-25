"""Operational endpoints: things you ask the app to do rather than read."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/poll")
def poll_now(request: Request) -> JSONResponse:
    """Run an ingestion cycle immediately.

    Answers 409 when a cycle is already under way. That is the honest reply rather than
    queueing a second one: two concurrent cycles would fetch every feed twice and race
    each other inserting the same items, and waiting for the running one adds nothing a
    caller could not get by asking again.
    """
    report = request.app.state.poll_runner.run()
    if report is None:
        return JSONResponse({"status": "already_running"}, status_code=status.HTTP_409_CONFLICT)
    return JSONResponse(
        {
            "status": "ok",
            "sources": report.sources,
            "stored": report.stored,
            "merged": report.merged,
            "failed": report.failed,
            "classified": report.classified,
            "unclassified": report.unclassified,
            "backlog": report.backlog,
            "pruned": report.pruned,
            "seconds": round(report.seconds, 1),
        }
    )
