"""Engine and sessions.

One SQLite file, one process writing it on a schedule and one serving pages from it.
That shapes two settings below that SQLite does not give you by default.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .settings import Settings

IN_MEMORY = ":memory:"


def _url_for(path: Path | str) -> str:
    if str(path) == IN_MEMORY:
        return "sqlite+pysqlite:///:memory:"
    return f"sqlite+pysqlite:///{Path(path).expanduser()}"


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Two defaults SQLite gets wrong for this shape of application.

    ``foreign_keys`` is OFF by default, for backwards compatibility with files written
    before SQLite enforced them. Without this, deleting a source leaves its items behind
    and every ``ondelete="CASCADE"`` in models.py is decoration.

    ``journal_mode=WAL`` lets the poller write while the web process reads. Under the
    default rollback journal a write blocks readers, so a poll cycle would freeze the
    page it is filling.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        # Wait for a lock rather than failing instantly; the two processes do collide.
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def create_db_engine(settings: Settings | None = None, *, path: Path | str | None = None) -> Engine:
    """Engine for ``FF_DB_PATH``, or for ``path`` when one is given.

    Pass ``path=":memory:"`` for tests. Creating an engine touches nothing: SQLAlchemy
    connects lazily, and the directory is made in ``init_schema`` at boot. That keeps
    importing this package free of side effects on disk.
    """
    target = path if path is not None else (settings or Settings()).db_path
    return create_engine(_url_for(target), future=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Sessions that do not expire objects on commit.

    The default re-fetches every attribute after a commit, which turns reading an item
    you just wrote into another round trip -- and raises if the session has closed.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """A session that commits on success and rolls back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_schema(engine: Engine) -> None:
    """Make the database usable: its directory, then any missing table.

    No Alembic for v1. ``create_all`` is enough for a single-user application, and it is
    idempotent, so it can run on every boot. The cost is that it only ever *adds*: a
    column that changes type or disappears needs a migration tool, and that decision gets
    revisited as its own task if the schema starts churning.

    Creating the parent directory belongs here rather than alongside the engine, so that
    a fresh deployment needs no manual step while merely importing the package writes
    nothing.
    """
    location = engine.url.database
    if location and location != IN_MEMORY:
        Path(location).expanduser().parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
