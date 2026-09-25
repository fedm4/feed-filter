"""Application factory and top-level routes."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .catalog import sync_catalog
from .db import create_db_engine, create_session_factory, init_schema, session_scope
from .routes import admin, feed, feedback, sources
from .scheduler import PollRunner, create_scheduler
from .settings import Settings
from .templating import STATIC_DIR, build_templates

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance: settings and the scheduler are
    wired in later, and tests need to build an app per configuration.
    """
    settings = settings or Settings()
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    poll_runner = PollRunner(session_factory, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # On boot, not at import: a fresh deployment should come up with a usable
        # database and no manual step, while importing this module -- which every test
        # does -- must not touch the disk.
        init_schema(engine)
        # The catalogue is the authority on which feeds exist, so it is applied on every
        # boot rather than once at install. Without this the sources table stays empty and
        # a scheduled poll fetches nothing -- quietly, which is the worst way to fail.
        with session_scope(session_factory) as session:
            report = sync_catalog(session, settings=settings)
        log.info(
            "catalog: %d added, %d updated, %d unchanged",
            report.added,
            report.updated,
            report.unchanged,
        )

        scheduler = create_scheduler(poll_runner, settings)
        scheduler.start()
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            # wait=True so a cycle in flight finishes its transaction rather than being
            # abandoned halfway through inserting a feed's items.
            scheduler.shutdown(wait=True)
            engine.dispose()

    app = FastAPI(title="feed-filter", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.poll_runner = poll_runner
    # Built once: it validates FF_UI_LANG, so an unknown language fails here rather than
    # on the first page someone opens.
    app.state.templates = build_templates(settings)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(feed.router)
    app.include_router(feedback.router)
    app.include_router(sources.router)
    app.include_router(admin.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
