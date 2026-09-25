"""The config editor: validation, export and import."""

import pytest
import yaml
from fastapi.testclient import TestClient

from feedfilter.config_file import DEFAULT_PATH, load_config
from feedfilter.main import create_app


@pytest.fixture
def client(app_env, tmp_path, monkeypatch):
    monkeypatch.setenv("FF_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app = create_app()
    with TestClient(app) as started:
        started.app = app
        started.config_path = tmp_path / "config.yaml"
        yield started


def test_the_page_shows_the_current_config(client) -> None:
    body = client.get("/config").text

    assert "interests" in body
    assert "sensationalism" in body


def test_env_thresholds_are_shown_read_only(client) -> None:
    """Otherwise editing them looks like it worked and silently did not."""
    body = client.get("/config").text

    assert "FF_MIN_RELEVANCE" in body
    assert "Fijado por una variable de entorno" in body


def test_saving_writes_the_file(client) -> None:
    config = load_config(DEFAULT_PATH)
    document = yaml.safe_dump(
        {
            "interests": "Only bread baking.",
            "topics": config.topics,
            "criteria": config.criteria,
        },
        allow_unicode=True,
    )

    client.post("/config", data={"document": document})

    assert client.config_path.exists()
    assert load_config(client.config_path).interests == "Only bread baking."


def test_a_saved_change_reaches_the_page(client) -> None:
    config = load_config(DEFAULT_PATH)
    document = yaml.safe_dump(
        {"interests": "Only bread baking.", "topics": config.topics, "criteria": config.criteria},
        allow_unicode=True,
    )
    client.post("/config", data={"document": document})

    assert "Only bread baking." in client.get("/config").text


@pytest.mark.parametrize(
    "document",
    ["interests: ''", "not: a config", "interests: x\ntopics: {}", ":::not yaml:::"],
    ids=["blank interests", "wrong keys", "no topics", "not yaml"],
)
def test_a_malformed_save_leaves_the_live_config_untouched(client, document) -> None:
    """The acceptance criterion: validate, then write, never the other way round."""
    before = load_config(settings=client.app.state.settings).interests

    response = client.post("/config", data={"document": document}, follow_redirects=True)

    assert response.status_code == 200
    assert load_config(settings=client.app.state.settings).interests == before
    assert not client.config_path.exists(), "nothing was written at all"


def test_export_then_import_reproduces_the_setup(client) -> None:
    """A clean install should come up identical from the exported file."""
    exported = client.get("/config/export")
    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]

    client.post(
        "/config/import", files={"file": ("config.yaml", exported.content, "application/x-yaml")}
    )

    assert load_config(client.config_path) == load_config(DEFAULT_PATH)


def test_a_malformed_upload_changes_nothing(client) -> None:
    client.post("/config/import", files={"file": ("config.yaml", b"::: nope :::", "text/yaml")})

    assert not client.config_path.exists()
