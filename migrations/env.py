"""Alembic environment for the procurement archive (ArchiveBase metadata only).

The URL is taken, in order, from: the `sqlalchemy.url` set programmatically (tests / hub.archive.migrate),
HUB_DATABASE_URL, DATABASE_URL. It is never logged.
"""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from hub.archive.models import ArchiveBase
from hub.db import normalise_url

config = context.config
target_metadata = ArchiveBase.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("HUB_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("set HUB_DATABASE_URL (or DATABASE_URL) to run archive migrations")
    return normalise_url(url)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      version_table="alembic_version_archive", render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is not None:
        _run(connectable)
        return
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run(conn)


def _run(conn) -> None:
    context.configure(connection=conn, target_metadata=target_metadata, version_table="alembic_version_archive",
                      render_as_batch=conn.dialect.name == "sqlite", compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
