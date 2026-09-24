"""Application factory and top-level routes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import create_db_engine, create_session_factory, init_schema
from .settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance: settings and the scheduler are
    wired in later, and tests need to build an app per configuration.
    """
    settings = settings or Settings()
    engine = create_db_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # On boot, not at import: a fresh deployment should come up with a usable
        # database and no manual step, while importing this module -- which every test
        # does -- must not touch the disk.
        init_schema(engine)
        yield
        engine.dispose()

    app = FastAPI(title="feed-filter", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
