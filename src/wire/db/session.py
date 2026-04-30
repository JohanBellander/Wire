"""SQLAlchemy engine + session factory.

Sync API. The bot is low-throughput; async DB doesn't earn its complexity.
Async callers wrap DB work with `asyncio.to_thread()`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DB_PATH = Path("/data/wire.db")


def _sqlite_url(path: Path) -> str:
    # Forward slashes work everywhere; sqlite:////absolute/path on Linux,
    # sqlite:///C:/path on Windows.
    p = str(path).replace("\\", "/")
    if p.startswith("/"):
        return f"sqlite:///{p}"
    return f"sqlite:///{p}"


def make_engine(path: Path | None = None, *, echo: bool = False) -> Engine:
    if path is None:
        path = Path(os.environ.get("WIRE_DB_PATH", DEFAULT_DB_PATH))
    url = _sqlite_url(path)
    engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"check_same_thread": False},
    )

    # Enable WAL + foreign keys per connection — sqlite forgets these on close.
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def init(path: Path | None = None, *, echo: bool = False) -> Engine:
    """Initialize the global engine + session factory. Idempotent."""
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine
    _engine = make_engine(path, echo=echo)
    _SessionFactory = sessionmaker(_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        return init()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        init()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session that commits on clean exit, rolls back on error."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_for_tests() -> None:
    """Drop the singleton engine so tests can swap in a fresh in-memory DB."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
