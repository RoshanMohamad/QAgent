"""Unit-level coverage for db_init.py's role bootstrap (ADR-0007) that doesn't
need a real Postgres - the live version of this is tests_integration/test_rls_isolation.py.

These tests cover the two failure modes a real database can't cheaply exercise:
a malformed role name reaching raw DDL, and the silent single-role fallback when
ADMIN_DATABASE_URL is never configured.
"""

from __future__ import annotations

import logging

import pytest

from qagent import db_init
from qagent.config import Settings


class _ExplodingEngine:
    """Any use at all is the bug under test - `ensure_app_role` must validate
    the role name before it ever opens a connection."""

    def begin(self):  # pragma: no cover - only invoked if the bug regresses
        raise AssertionError("admin engine must not be touched for an unsafe role name")


@pytest.mark.parametrize(
    "bad_username",
    [
        "qagent-app",  # hyphen: not in [a-z_][a-z0-9_]*
        "Qagent",  # uppercase
        "qagent app",  # space
        "qagent'; DROP TABLE users;--",  # the thing this check exists for
        "1qagent",  # leading digit
    ],
)
def test_ensure_app_role_rejects_unsafe_role_name(monkeypatch, bad_username) -> None:
    settings = Settings(database_url=f"postgresql+psycopg://{bad_username}:x@localhost/db")
    monkeypatch.setattr(db_init, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="not a safe role identifier"):
        db_init.ensure_app_role(_ExplodingEngine())


def test_ensure_app_role_accepts_safe_role_name(monkeypatch) -> None:
    """The regex isn't accidentally rejecting the common case."""
    settings = Settings(database_url="postgresql+psycopg://qagent_app:x@localhost/db")
    monkeypatch.setattr(db_init, "get_settings", lambda: settings)

    with pytest.raises(AssertionError):  # _ExplodingEngine.begin() fires *after* validation
        db_init.ensure_app_role(_ExplodingEngine())


def test_admin_engine_falls_back_to_database_url_and_warns(monkeypatch, caplog) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://qagent:x@localhost/db", admin_database_url=None
    )
    monkeypatch.setattr(db_init, "get_settings", lambda: settings)

    with caplog.at_level(logging.WARNING):
        engine = db_init._admin_engine()

    assert str(engine.url) == "postgresql+psycopg://qagent:***@localhost/db"
    assert any("ADMIN_DATABASE_URL is not set" in record.message for record in caplog.records)


def test_admin_engine_prefers_explicit_admin_url(monkeypatch, caplog) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://qagent_app:x@localhost/db",
        admin_database_url="postgresql+psycopg://qagent:y@localhost/db",
    )
    monkeypatch.setattr(db_init, "get_settings", lambda: settings)

    with caplog.at_level(logging.WARNING):
        engine = db_init._admin_engine()

    assert "qagent_app" not in str(engine.url)
    assert not any("ADMIN_DATABASE_URL is not set" in r.message for r in caplog.records)
