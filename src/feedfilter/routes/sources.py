"""Choosing what to read from.

Toggling writes to the database rather than to the catalogue file. The file is the
authority on which feeds *exist*; whether one is switched on is state, which is why D1's
sync deliberately never re-enables something turned off here.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from ..canonical import canonicalize
from ..db import session_scope
from ..i18n import Translator
from ..models import Source
from ..opml import OpmlError, parse_opml
from ..templating import render

router = APIRouter(tags=["sources"])

#: Failures in a row before a feed is called dead in the list. Enough that a flaky
#: afternoon does not raise an alarm, few enough that a week of silence does.
DEAD_AFTER = 5


def _redirect(message: str | None = None) -> RedirectResponse:
    target = "/sources" + (f"?flash={message}" if message else "")
    return RedirectResponse(target, status_code=303)


@router.get("/sources")
def sources_page(request: Request, flash: str | None = None):
    with session_scope(request.app.state.session_factory) as session:
        rows = list(session.scalars(select(Source).order_by(Source.topic, Source.name)))
        grouped: dict[str, list[Source]] = {}
        for source in rows:
            grouped.setdefault(source.topic or "other", []).append(source)

    return render(
        request.app.state.templates,
        request,
        "sources.html",
        page="sources",
        grouped=grouped,
        total=len(rows),
        enabled=sum(1 for source in rows if source.enabled),
        dead_after=DEAD_AFTER,
        flash=flash,
    )


@router.post("/sources/toggle")
def toggle(request: Request, source_id: int = Form(...)):
    with session_scope(request.app.state.session_factory) as session:
        source = session.get(Source, source_id)
        if source is not None:
            source.enabled = not source.enabled
    return _redirect()


@router.post("/sources/add")
def add(request: Request, url: str = Form(...), name: str = Form("")):
    translator: Translator = request.app.state.templates.env.globals["t"]
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return _redirect(translator("sources.invalid_url"))

    with session_scope(request.app.state.session_factory) as session:
        if session.scalars(select(Source).where(Source.url == url)).first() is not None:
            return _redirect(translator("sources.already_present"))
        label = name.strip() or canonicalize(url)
        session.add(Source(name=label, url=url, lang="und", topic="other"))

    return _redirect(translator("sources.added", name=label))


@router.post("/sources/import")
async def import_opml(request: Request, file: UploadFile = File(...)):
    """Add every feed in an OPML export, skipping the ones already present."""
    translator: Translator = request.app.state.templates.env.globals["t"]
    try:
        feeds = parse_opml(await file.read())
    except OpmlError:
        return _redirect(translator("sources.import_failed"))

    added = skipped = 0
    with session_scope(request.app.state.session_factory) as session:
        known = {source.url for source in session.scalars(select(Source))}
        for feed in feeds:
            if feed.url in known:
                skipped += 1
                continue
            # Language unknown: the importing reader does not say, and Laya routes per
            # article anyway, so guessing here would buy nothing.
            session.add(Source(name=feed.name, url=feed.url, lang="und", topic="other"))
            known.add(feed.url)
            added += 1

    return _redirect(translator("sources.imported", added=added, skipped=skipped))
