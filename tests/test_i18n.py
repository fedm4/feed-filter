"""Translation lookup, fallbacks, and the two ways it should fail loudly."""

import pytest

from feedfilter.i18n import (
    DEFAULT_LANG,
    MissingTranslation,
    Translator,
    UnknownLanguage,
    available,
    catalogue,
    translator_for,
)
from feedfilter.settings import Settings


def test_spanish_is_the_default() -> None:
    assert DEFAULT_LANG == "es"
    assert translator_for().lang == "es"


def test_both_catalogues_ship() -> None:
    assert available() == ["en", "es"]


def test_the_language_comes_from_settings() -> None:
    """The acceptance criterion: English with no code change."""
    spanish = translator_for(Settings(ui_lang="es"))
    english = translator_for(Settings(ui_lang="en"))

    assert spanish("nav.sources") == "Fuentes"
    assert english("nav.sources") == "Sources"


def test_an_unknown_language_is_refused() -> None:
    """At startup, not on the first page render."""
    with pytest.raises(UnknownLanguage, match="klingon"):
        translator_for(Settings(ui_lang="klingon"))


def test_a_missing_key_raises_rather_than_rendering_blank() -> None:
    """A blank label is invisible in development and very visible to whoever uses it."""
    with pytest.raises(MissingTranslation):
        Translator("es")("nav.does_not_exist")


def test_placeholders_are_filled() -> None:
    assert "3" in Translator("en")("feed.showing", shown=3, total=9)
    assert "9" in Translator("en")("feed.showing", shown=3, total=9)


def test_a_missing_placeholder_value_raises() -> None:
    with pytest.raises(MissingTranslation):
        Translator("en")("feed.showing", shown=3)


def test_a_gap_in_a_translation_falls_back_to_the_default(monkeypatch) -> None:
    catalogue.cache_clear()
    monkeypatch.setitem(catalogue("en"), "nav.feed", None)
    del catalogue("en")["nav.feed"]

    assert Translator("en")("nav.feed") == Translator("es")("nav.feed")
    catalogue.cache_clear()


def test_model_labels_are_translated_for_display() -> None:
    """Stored as English identifiers, shown in the reader's language."""
    assert Translator("es").label("content_type", "opinion") == "Opinión"
    assert Translator("en").label("content_type", "opinion") == "Opinion"
    assert Translator("es").label("sensationalism", "tabloid") == "Amarillista"


def test_an_unknown_identifier_shows_as_itself() -> None:
    """A topic someone added to their config should appear, not break the page."""
    assert Translator("es").label("topic", "gardening") == "gardening"


def test_every_key_exists_in_both_catalogues() -> None:
    """Otherwise English silently renders Spanish, which reads like a bug."""
    catalogue.cache_clear()
    assert set(catalogue("en")) == set(catalogue("es"))
