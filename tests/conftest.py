import os

import pytest


@pytest.fixture(autouse=True)
def isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop ambient FF_ variables for every test.

    The Makefile does `-include .env` and `export`, so whatever a developer keeps in
    their own .env reaches `make check`. Without this, a local FF_PORT would make the
    default assertions fail on their machine and pass in CI.
    """
    for name in list(os.environ):
        if name.startswith("FF_"):
            monkeypatch.delenv(name, raising=False)
