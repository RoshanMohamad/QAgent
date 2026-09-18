"""RLS actually isolates tenants when the app connects as the role ADR-0007
introduces -- not just when a superuser's queries happen to filter correctly.

This is written from a real, reproduced failure: before ADR-0007, `DATABASE_URL`
and the bootstrap connection were the same role, and the official postgres
Docker image always makes that role a superuser. Superusers bypass row-level
security unconditionally, `FORCE` or not, so every policy `db_init.apply_rls`
installed was silently inert. `test_app_role_is_not_privileged` below is the
regression test for that specific failure mode; the rest exercise the isolation
it makes possible.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError, ProgrammingError
from sqlalchemy.orm import sessionmaker

from qagent import models
from qagent.db import set_tenant


@pytest.fixture
def Session(app_engine):  # noqa: N802 - matches sessionmaker's own convention
    return sessionmaker(bind=app_engine, autoflush=False, expire_on_commit=False, future=True)


def _make_org(session, slug: str) -> models.Organization:
    org = models.Organization(id=uuid.uuid4(), name=slug, slug=slug)
    session.add(org)
    session.commit()
    return org


def test_app_role_is_not_privileged(admin_engine) -> None:
    """The regression test: whatever role DATABASE_URL names must never be able
    to bypass the policies apply_rls just installed."""
    from sqlalchemy.engine import make_url

    from qagent.config import get_settings

    role = make_url(get_settings().database_url).username
    with admin_engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :role"
            ),
            {"role": role},
        ).one()
    assert row.rolsuper is False
    assert row.rolbypassrls is False


def test_own_organization_is_visible(Session) -> None:
    session = Session()
    org = _make_org(session, "org-visible")
    set_tenant(session, org.id)
    project = models.Project(org_id=org.id, name="p1")
    session.add(project)
    session.flush()

    visible = session.query(models.Project).all()

    assert [p.id for p in visible] == [project.id]
    session.close()


def test_other_organizations_projects_are_invisible(Session) -> None:
    writer = Session()
    org_a = _make_org(writer, "org-a")
    org_b = _make_org(writer, "org-b")
    set_tenant(writer, org_a.id)
    writer.add(models.Project(org_id=org_a.id, name="a's project"))
    writer.commit()
    writer.close()

    reader = Session()
    set_tenant(reader, org_b.id)

    visible = reader.query(models.Project).all()

    assert visible == []
    reader.close()


def test_cross_tenant_insert_is_rejected(Session) -> None:
    writer = Session()
    org_a = _make_org(writer, "org-cross-a")
    org_b = _make_org(writer, "org-cross-b")
    writer.close()

    session = Session()
    set_tenant(session, org_b.id)
    # WITH CHECK, not just USING: a row claiming org_a's id while bound to org_b
    # must be rejected at write time, not merely hidden from later reads.
    session.add(models.Project(org_id=org_a.id, name="rogue"))

    with pytest.raises(ProgrammingError):
        session.commit()
    session.rollback()
    session.close()


def test_no_tenant_bound_fails_closed(Session) -> None:
    """`current_setting('qagent.current_org', true)` with nothing ever set in
    this transaction is an empty string, not a wildcard: the USING clause casts
    it to uuid and the query errors rather than returning every tenant's rows.
    Failing loudly beats failing open."""
    writer = Session()
    org = _make_org(writer, "org-unbound")
    set_tenant(writer, org.id)
    writer.add(models.Project(org_id=org.id, name="p"))
    writer.commit()
    writer.close()

    unbound = Session()
    with pytest.raises(DataError):
        unbound.query(models.Project).all()
    unbound.rollback()
    unbound.close()


def test_table_owner_is_not_the_app_role(admin_engine, app_engine) -> None:
    """`ensure_app_role` never makes the app role own a table: FORCE ROW LEVEL
    SECURITY governs an owner too, but a role that owns nothing has one less way
    to have ever been exempted from its own policies in the first place."""
    from sqlalchemy.engine import make_url

    from qagent.config import get_settings

    app_role = make_url(get_settings().database_url).username
    with admin_engine.connect() as connection:
        owner = connection.execute(
            text("SELECT tableowner FROM pg_tables WHERE tablename = 'projects'")
        ).scalar_one()
    assert owner != app_role
