"""Database engine, session lifecycle and the tenant guard.

Multi-tenancy is enforced in Postgres via row-level security rather than by
remembering to add `WHERE org_id = ...` to every query. `session_scope` sets the
per-transaction tenant so RLS policies apply to everything the request touches.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from qagent.config import get_settings

_settings = get_settings()

engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def set_tenant(session: Session, org_id: UUID | str) -> None:
    """Bind the current transaction to one organization for RLS."""
    session.execute(
        text("SELECT set_config('qagent.current_org', :org, true)"),
        {"org": str(org_id)},
    )


@contextmanager
def session_scope(org_id: UUID | str | None = None) -> Iterator[Session]:
    session = SessionLocal()
    try:
        if org_id is not None:
            set_tenant(session, org_id)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Tenant binding is applied by the auth dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
