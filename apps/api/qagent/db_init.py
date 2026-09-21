"""Schema bootstrap, the low-privilege application role, and row-level security.

Alembic owns migrations from the first schema change onward; this module exists to
create the initial schema, provision the role the API/worker actually connect as,
and install the RLS policies. Tenant isolation enforced by the database cannot be
forgotten at a call site, which is why it is applied here rather than left to
application code -- and, per ADR-0007, why it is applied to a role that RLS can
actually constrain, not to whatever role happened to run this script.

Everything here runs against an *admin* connection (a real Postgres superuser, or
at least a role that owns these tables) - `Settings.admin_database_url`, never
`Settings.database_url`. The application and worker never see the admin DSN.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

from qagent.config import get_settings
from qagent.models import TENANT_TABLES

logger = logging.getLogger(__name__)

#: Applied per tenant table. USING governs reads, WITH CHECK governs writes, so a
#: row can neither be read nor created outside the caller's organization. FORCE
#: additionally applies this to the table's *owner* - necessary but not sufficient:
#: a superuser bypasses RLS regardless of FORCE, which is exactly why the role this
#: module creates for runtime use is never one (see `ensure_app_role`, ADR-0007).
_POLICY = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};
CREATE POLICY {table}_tenant_isolation ON {table}
    USING (org_id = current_setting('qagent.current_org', true)::uuid)
    WITH CHECK (org_id = current_setting('qagent.current_org', true)::uuid);
"""

#: Postgres unquoted identifiers: this is deliberately stricter than what Postgres
#: itself accepts, because the role name is about to be interpolated into DDL that
#: cannot be parameterised (CREATE ROLE takes no bind parameters for its name).
#: The value comes from configuration, not a request, but an identifier this
#: routine cannot prove safe is refused rather than trusted.
_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _admin_engine() -> Engine:
    settings = get_settings()
    if settings.admin_database_url:
        return create_engine(settings.admin_database_url, future=True)

    logger.warning(
        "ADMIN_DATABASE_URL is not set; bootstrapping schema/roles/RLS using the "
        "same role the application connects as (DATABASE_URL). If that role is a "
        "Postgres superuser - which the official postgres image's POSTGRES_USER "
        "always is - every row-level security policy below is silently bypassed "
        "for every query the app makes. See ADR-0007. Set ADMIN_DATABASE_URL to a "
        "real superuser DSN and point DATABASE_URL at a separate, unprivileged "
        "role in any deployment where tenant isolation matters."
    )
    return create_engine(settings.database_url, future=True)


#: The revision describing the schema as it was before Alembic was introduced.
#: A database that already has tables but no ``alembic_version`` is stamped here
#: and then upgraded, which is only sound because this project has exactly one
#: pre-migration schema generation. If that ever stops being true, this becomes
#: a lie and the stamp has to be done deliberately per deployment instead.
BASELINE_REVISION = "be76ba4d4b4c"

_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def _alembic_config(bind: Engine) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", bind.url.render_as_string(hide_password=False))
    return config


def create_schema(bind: Engine) -> None:
    """Bring the schema to head with Alembic.

    This replaced ``Base.metadata.create_all``, and the reason is worth stating:
    ``create_all`` creates *missing tables* and nothing else. It will not add a
    column to a table that already exists, so every schema change after the
    first one silently did nothing on any database that had already been
    created, and the failure surfaced later as "column does not exist" at
    runtime rather than at deploy time.

    A database with tables but no ``alembic_version`` predates migrations
    entirely; it is stamped at the baseline and then upgraded, so existing
    deployments adopt migrations without being recreated.
    """
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    config = _alembic_config(bind)

    if tables and "alembic_version" not in tables:
        logger.warning(
            "database has %d table(s) but no alembic_version: assuming the "
            "pre-migration schema and stamping %s before upgrading",
            len(tables),
            BASELINE_REVISION,
        )
        command.stamp(config, BASELINE_REVISION)

    command.upgrade(config, "head")
    logger.info("schema at head")


def ensure_app_role(admin: Engine) -> str:
    """Create, or reset, the low-privilege role the API and worker connect as.

    The role to create is read from ``DATABASE_URL`` itself - the app's own
    connection string - rather than from a second setting, so there is exactly
    one place that says "this is who the app connects as" and nothing can drift
    between the two. It is explicitly stripped of superuser, BYPASSRLS, and the
    ability to create databases or other roles every time this runs, so a manual
    `ALTER ROLE` on a long-lived database can never quietly widen it back out.

    It is also never made the owner of a table: `create_schema` runs on the admin
    connection, so ownership stays there, and this role receives only the DML
    grants (SELECT/INSERT/UPDATE/DELETE) it needs - never DDL. An owner is exempt
    from its own RLS policies unless FORCE is set (it is, here); a non-owner
    without BYPASSRLS is constrained by RLS regardless.
    """
    settings = get_settings()
    app_url = make_url(settings.database_url)
    role = app_url.username
    password = app_url.password or ""

    if not role or not _SAFE_IDENTIFIER.match(role):
        raise ValueError(
            f"DATABASE_URL's username {role!r} is not a safe role identifier "
            "(expected ^[a-z_][a-z0-9_]*$); refusing to run role-bootstrap DDL."
        )

    # DDL can't bind a parameter in place of an identifier or a password literal,
    # so the identifier is validated above and the literal is escaped here as
    # Postgres itself defines escaping for a quoted string (doubling the quote).
    escaped_password = password.replace("'", "''")

    with admin.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        ).first()

        privileges = "NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION"
        if exists is None:
            connection.execute(
                text(f"CREATE ROLE {role} LOGIN PASSWORD '{escaped_password}' {privileges}")
            )
            logger.info("created application role %r", role)
        else:
            # Idempotent, and deliberately corrective: re-running this must reset
            # the password to match configuration and re-strip every privilege
            # above, not just create the role once and trust it stays narrow.
            connection.execute(
                text(f"ALTER ROLE {role} WITH PASSWORD '{escaped_password}' {privileges}")
            )
            logger.info("application role %r already existed; password and privileges reset", role)

        connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
        )
        connection.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"))
        # Covers tables Alembic adds later without a manual re-grant, since those
        # too are created by the admin connection.
        connection.execute(
            text(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
            )
        )

    return role


def apply_rls(admin: Engine) -> None:
    """Install tenant isolation policies. See `_POLICY` for what FORCE buys and
    does not buy - the app role having no BYPASSRLS is what makes it not buy.
    """
    with admin.begin() as connection:
        for table in TENANT_TABLES:
            connection.execute(text(_POLICY.format(table=table)))
    logger.info("row-level security applied to %d tables", len(TENANT_TABLES))


def enable_pgvector(admin: Engine) -> None:
    """Upgrade code_chunks.embedding to a real vector column, if possible.

    Runs here rather than in the model because ``CREATE EXTENSION`` needs
    privileges the application role deliberately lacks (ADR-0007), and because
    stock ``postgres:16-alpine`` - the image this project's compose file ships -
    has no vector extension at all. When it is absent the schema is still
    correct and similarity search runs in Python (modules/rag/store.py).
    """
    from sqlalchemy.orm import Session

    from qagent.config import get_settings
    from qagent.modules.rag.store import ensure_vector_support

    with Session(admin) as session:
        support = ensure_vector_support(
            session, dimensions=get_settings().qagent_embedding_dimensions
        )
    logger.info("vector support: %s", support.to_dict())


def init() -> None:
    admin = _admin_engine()
    create_schema(admin)
    enable_pgvector(admin)
    ensure_app_role(admin)
    apply_rls(admin)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init()
    print(f"schema ready, RLS applied to {len(TENANT_TABLES)} tables")
