from __future__ import annotations

import datetime as dt
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    """Naive UTC timestamp (all DB times are UTC)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def normalise_url(url: str) -> str:
    """Hosts (Render, Neon, Heroku-style) hand out postgres:// URLs; SQLAlchemy needs the driver name."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str):
    url = normalise_url(url)
    kwargs = {}
    if url.startswith("postgresql"):
        kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=300,
                      # Neon's pooled endpoint is PgBouncer in transaction mode: server-side prepared statements
                      # may land on another backend, so psycopg must not auto-prepare. connect_timeout covers cold starts.
                      connect_args={"prepare_threshold": None, "connect_timeout": 15})
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    eng = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    return eng


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def configure(url: str):
    """Re-point the app at another database (tests, staging)."""
    global engine
    engine = make_engine(url)
    SessionLocal.configure(bind=engine)
    return engine


def init_db():
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
