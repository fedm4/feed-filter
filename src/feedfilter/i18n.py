"""Interface strings, chosen by `FF_UI_LANG`.

This exists before the first template on purpose: retrofitting it afterwards means
touching every template again.

A missing key raises rather than rendering blank. A blank label in a UI is nearly
invisible during development and very visible to whoever uses it later.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

from .settings import Settings

LOCALES_DIR = Path(__file__).parent / "locales"
#: Falls back here for a key a translation has not covered yet.
DEFAULT_LANG = "es"


class MissingTranslation(KeyError):
    """A key the catalogue does not have. Raised so it cannot render as nothing."""


class UnknownLanguage(ValueError):
    """FF_UI_LANG names a catalogue that does not exist."""


def available() -> list[str]:
    return sorted(path.stem for path in LOCALES_DIR.glob("*.yaml"))


def _flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """`{"nav": {"feed": "Feed"}}` becomes `{"nav.feed": "Feed"}`."""
    flat: dict[str, str] = {}
    for key, value in tree.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = str(value)
    return flat


@functools.lru_cache(maxsize=8)
def catalogue(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.yaml"
    if not path.exists():
        raise UnknownLanguage(f"no catalogue for {lang!r}; have {', '.join(available())}")
    return _flatten(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


class Translator:
    """Looks up one language, falling back to the default for gaps."""

    def __init__(self, lang: str | None = None, *, settings: Settings | None = None) -> None:
        self.lang = lang or (settings or Settings()).ui_lang
        self._strings = catalogue(self.lang)
        self._fallback = catalogue(DEFAULT_LANG) if self.lang != DEFAULT_LANG else {}

    def __call__(self, key: str, **values: Any) -> str:
        """The string for `key`, with `%(name)s` placeholders filled from `values`."""
        text = self._strings.get(key) or self._fallback.get(key)
        if text is None:
            raise MissingTranslation(key)
        if not values:
            return text
        try:
            return text % values
        except KeyError as exc:
            raise MissingTranslation(f"{key}: no value for {exc}") from exc

    def label(self, question: str, identifier: str) -> str:
        """The display name of a model-facing identifier, e.g. ("content_type", "opinion").

        Unknown identifiers come back as themselves rather than raising: a new topic in
        someone's config.yaml should show up in the UI, not break the page.
        """
        try:
            return self(f"{question}.{identifier}")
        except MissingTranslation:
            return identifier


def translator_for(settings: Settings | None = None) -> Translator:
    """Fails at startup on an unknown FF_UI_LANG rather than on the first page render."""
    return Translator(settings=settings)
