from datetime import time

import pytest
from pydantic import ValidationError

from feedfilter.settings import OpinionPolicy, Relevance, Settings


def test_defaults_are_the_documented_ones() -> None:
    settings = Settings()

    assert settings.tz == "Europe/Madrid"
    assert settings.ui_lang == "es"
    assert settings.port == 8080
    assert settings.min_relevance is Relevance.MARGINAL
    assert settings.opinion_policy is OpinionPolicy.TAG


def test_environment_overrides_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FF_PORT", "9999")
    monkeypatch.setenv("FF_UI_LANG", "en")

    settings = Settings()

    assert settings.port == 9999
    assert settings.ui_lang == "en"


def test_digest_times_accepts_several_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FF_DIGEST_TIMES", "08:30, 20:00")

    assert Settings().digest_schedule == [time(8, 30), time(20, 0)]


def test_timezone_resolves_to_a_zoneinfo() -> None:
    assert Settings().timezone.key == "Europe/Madrid"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("FF_OPINION_POLICY", "silently_drop"),
        ("FF_TZ", "Mars/Olympus"),
        ("FF_DIGEST_TIMES", "half past eight"),
        ("FF_LAYA_BATCH_SIZE", "0"),
        ("FF_PORT", "70000"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings()
