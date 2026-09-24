"""Application factory and top-level routes."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance: settings and the scheduler are
    wired in later, and tests need to build an app per configuration.
    """
    app = FastAPI(title="feed-filter")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
