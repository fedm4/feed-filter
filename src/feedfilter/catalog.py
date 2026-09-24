"""The curated source list: loading it, and syncing it into the database.

The catalogue is a file rather than rows someone typed into a form, so a feed's URL is
decided in one place and an install starts from something known to work.

Which file: ``catalog.example.yaml`` ships beside the code and changes only by pull
request. The list actually polled is ``FF_CATALOG_PATH``, by default ``data/catalog.yaml``,
which is yours -- untracked, alongside the database, and never argued with by a git pull.
It has to be copied before first use, deliberately: a curated feed list is a decision, and
starting from a silent default hides that a decision was made at all.

The database is still the authority on *state* -- ETags, failure counts, and whether the
user has switched a feed off from the UI. So syncing is deliberately narrow: it adds
feeds that are new and refreshes the descriptive fields of ones already there, and it
never deletes and never resurrects something the user turned off.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Source
from .settings import Settings

#: Ships with the code. Read directly only to validate or to seed a copy.
EXAMPLE_PATH = Path(__file__).parent / "catalog.example.yaml"

# Kept in step with the Source columns a catalogue entry is allowed to set. Anything else
# in the file is a typo, and a typo that is silently ignored is a feed that never polls.
_FIELDS = {"name", "url", "lang", "country", "topic", "enabled"}


class CatalogError(ValueError):
    """The catalogue file is malformed. Raised with the entry that caused it."""


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    name: str
    url: str
    lang: str
    country: str | None = None
    topic: str | None = None
    enabled: bool = True


def _entry_from(raw: Any, index: int) -> CatalogEntry:
    where = f"entry {index}"
    if not isinstance(raw, dict):
        raise CatalogError(f"{where}: expected a mapping, got {type(raw).__name__}")

    unknown = set(raw) - _FIELDS
    if unknown:
        raise CatalogError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    for required in ("name", "url", "lang"):
        if not raw.get(required):
            raise CatalogError(f"{where}: missing {required}")
    if not str(raw["url"]).startswith(("http://", "https://")):
        raise CatalogError(f"{where} ({raw['name']}): url must be http(s), got {raw['url']!r}")

    return CatalogEntry(
        name=str(raw["name"]),
        url=str(raw["url"]),
        lang=str(raw["lang"]),
        country=str(raw["country"]) if raw.get("country") else None,
        topic=str(raw["topic"]) if raw.get("topic") else None,
        enabled=bool(raw.get("enabled", True)),
    )


def catalog_path(settings: Settings | None = None) -> Path:
    """Where the polled catalogue lives: ``FF_CATALOG_PATH``."""
    return (settings or Settings()).catalog_path


def load_catalog(
    path: Path | None = None, *, settings: Settings | None = None
) -> list[CatalogEntry]:
    """Parse and validate the catalogue. Raises ``CatalogError`` on anything malformed.

    Missing is an error rather than a fallback to the example. Falling back would mean a
    deployment quietly polling a list nobody chose, and a typo in ``FF_CATALOG_PATH``
    looking exactly like success.
    """
    target = path if path is not None else catalog_path(settings)
    if not target.exists():
        raise CatalogError(
            f"no catalogue at {target}. Copy the example to start from it:\n"
            f"    mkdir -p {target.parent} && cp {EXAMPLE_PATH} {target}"
        )
    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "sources" not in document:
        raise CatalogError("catalogue must be a mapping with a 'sources' key")

    raw_entries = document["sources"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise CatalogError("'sources' must be a non-empty list")

    entries = [_entry_from(raw, index) for index, raw in enumerate(raw_entries, start=1)]

    seen: dict[str, int] = {}
    for index, entry in enumerate(entries, start=1):
        if entry.url in seen:
            raise CatalogError(f"entry {index} ({entry.name}): url repeats entry {seen[entry.url]}")
        seen[entry.url] = index
    return entries


@dataclass(frozen=True, slots=True)
class SyncReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.added + self.updated + self.unchanged


def sync_catalog(
    session: Session,
    entries: list[CatalogEntry] | None = None,
    *,
    settings: Settings | None = None,
) -> SyncReport:
    """Bring the ``sources`` table in line with the catalogue, conservatively.

    New entries are inserted. Existing ones get their descriptive fields refreshed, since
    the file is where a name or a topic is decided.

    Two things are deliberately left alone. ``enabled`` is only ever set when a source is
    first inserted: switching a feed off is something the user does from the UI, and a
    sync that flipped it back on would undo that on every boot. And a source missing from
    the file is never deleted -- it would take its items, verdicts and labels with it
    through the foreign key, which is far too much to lose over an edit to a YAML file.
    """
    entries = entries if entries is not None else load_catalog(settings=settings)
    existing = {source.url: source for source in session.scalars(select(Source))}

    added = updated = unchanged = 0
    for entry in entries:
        source = existing.get(entry.url)
        if source is None:
            session.add(
                Source(
                    name=entry.name,
                    url=entry.url,
                    lang=entry.lang,
                    country=entry.country,
                    topic=entry.topic,
                    enabled=entry.enabled,
                )
            )
            added += 1
            continue

        changes = {
            "name": entry.name,
            "lang": entry.lang,
            "country": entry.country,
            "topic": entry.topic,
        }
        if all(getattr(source, field) == value for field, value in changes.items()):
            unchanged += 1
            continue
        for field, value in changes.items():
            setattr(source, field, value)
        updated += 1

    session.flush()
    return SyncReport(added=added, updated=updated, unchanged=unchanged)
