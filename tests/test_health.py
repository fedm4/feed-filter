from fastapi.testclient import TestClient
from sqlalchemy import inspect

from feedfilter.main import create_app


def test_healthz_reports_ok() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_boot_creates_a_usable_database(tmp_path, monkeypatch, app_env) -> None:
    """A fresh deployment should come up with no manual step: no mkdir, no create table."""
    target = tmp_path / "does" / "not" / "exist" / "feedfilter.db"
    monkeypatch.setenv("FF_DB_PATH", str(target))
    assert not target.parent.exists()

    app = create_app()
    # The engine alone must touch nothing; the work belongs to the lifespan.
    assert not target.parent.exists()

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        tables = set(inspect(app.state.engine).get_table_names())

    assert target.exists()
    assert {"sources", "items", "verdicts", "labels"} <= tables
