"""Alembic environment.

Two things here differ from the scaffold alembic generates, and both matter:

**The URL comes from ``ADMIN_DATABASE_URL``, not from alembic.ini.** Migrations
issue DDL, which the application role deliberately cannot do (ADR-0007). Reading
it from the environment also means one image migrates every deployment without
editing a file that would then have to be kept out of version control.

**``compare_type`` is on.** The ``code_chunks.embedding`` column is JSON or
``vector(n)`` depending on ``QAGENT_VECTOR_BACKEND``, and without type
comparison an autogenerate run against a pgvector deployment would silently
produce an empty migration.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from qagent.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("ADMIN_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "ADMIN_DATABASE_URL (preferred) or DATABASE_URL must be set to run migrations"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
