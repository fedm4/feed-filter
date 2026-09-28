import os
from itertools import count

import pytest
from sqlalchemy.orm import Session

from feedfilter.db import create_db_engine, create_session_factory, init_schema
from feedfilter.models import Item, Label, Source, Verdict


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
    # And retry without waiting: the backoff is real time, and the suite is not here to
    # spend it. The delays themselves are asserted in test_classify_resilience.
    monkeypatch.setenv("FF_LAYA_RETRY_BACKOFF_S", "0")


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Point a real app at a database and a catalogue of its own.

    Both matter. Without FF_CATALOG_PATH the app boots against whatever catalogue happens
    to sit in ./data on the machine running the tests -- which passes on a developer's
    machine and fails in CI, where that file does not exist.
    """
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        "sources:\n  - name: Seeded\n    url: https://seeded.example/feed\n    lang: en\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FF_DB_PATH", str(tmp_path / "ff.db"))
    monkeypatch.setenv("FF_CATALOG_PATH", str(catalog))
    return catalog


@pytest.fixture
def factory():
    """A session factory over an empty in-memory database."""
    engine = create_db_engine(path=":memory:")
    init_schema(engine)
    return create_session_factory(engine)


# Rows need to differ from each other more often than they need to be predictable, and
# url_hash is unique by constraint. A counter gives every builder call its own values
# without the caller having to invent any.
_seq = count(1)


def make_source(session: Session, **overrides) -> Source:
    """A feed. Override any field; the rest get workable defaults."""
    n = next(_seq)
    source = Source(
        **{
            "name": f"Example {n}",
            "url": f"https://example.com/{n}/feed",
            "lang": "en",
            **overrides,
        }
    )
    session.add(source)
    session.flush()
    return source


def make_item(session: Session, source: Source, **overrides) -> Item:
    """An article belonging to ``source``.

    Note these builders write rows directly and are meant to keep doing so once D1 and
    D3 bring real creators. A production creator stamps ``fetched_at`` with now and runs
    canonicalisation and dedup on the way past; a retention test needs an item fetched
    400 days ago, which the real path cannot produce. Different jobs, both needed.
    """
    n = next(_seq)
    item = Item(
        **{
            "source_id": source.id,
            "url": f"https://example.com/{n}?utm_source=rss",
            "canonical_url": f"https://example.com/{n}",
            "url_hash": f"hash-{n}",
            "title": f"Headline {n}",
            **overrides,
        }
    )
    session.add(item)
    session.flush()
    return item


def make_verdict(session: Session, item: Item, **overrides) -> Verdict:
    """What the classifier said about ``item``."""
    verdict = Verdict(
        **{
            "item_id": item.id,
            "question": "kind",
            "distribution": {"news": 0.8, "opinion": 0.2},
            "top_label": "news",
            **overrides,
        }
    )
    session.add(verdict)
    session.flush()
    return verdict


def make_label(session: Session, item: Item, **overrides) -> Label:
    """What the user said about ``item``."""
    label = Label(
        **{
            "item_id": item.id,
            "dimension": "relevance",
            "value": "essential",
            **overrides,
        }
    )
    session.add(label)
    session.flush()
    return label
