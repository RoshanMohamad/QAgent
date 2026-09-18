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
    """Bind the *current transaction* to one organization for RLS.

    The final ``true`` makes this ``SET LOCAL``, not ``SET``: it lives only until
    the transaction ends, then Postgres resets it to an empty string rather than
    leaving it unset. That is deliberate - a pooled connection must never carry
    one request's tenant into the next simply because nobody reset it - but it
    means a call site that commits and then issues another query in the *same*
    session must call this again first, or every RLS-guarded query in that new
    transaction fails closed (an invalid ``''::uuid`` cast, not silently seeing
    every tenant's rows - see ADR-0007). `session_scope` and every FastAPI route
    here call this exactly once per transaction for that reason; if you add a
    call site that commits mid-session, call this again immediately after.
    """
    session.execute(
        text("SELECT set_config('qagent.current_org', :org, true)"),
        {"org": str(org_id)},
    )


@contextmanager
def session_scope(org_id: UUID | str | None = None) -> Iterator[Session]:
    """One session, one transaction, one tenant. Do not call ``session.commit()``
    yourself inside this block and then keep querying - see `set_tenant`."""
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
