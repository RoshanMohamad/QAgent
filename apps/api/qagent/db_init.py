"""Schema bootstrap and row-level security policies.

Alembic owns migrations from the first schema change onward; this module exists to
create the initial schema and, more importantly, to install the RLS policies. Tenant
isolation enforced by the database cannot be forgotten at a call site, which is why
it is applied here rather than left to application code.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from qagent.db import engine
from qagent.models import TENANT_TABLES, Base

logger = logging.getLogger(__name__)

#: Applied per tenant table. USING governs reads, WITH CHECK governs writes, so a
#: row can neither be read nor created outside the caller's organization.
_POLICY = """
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};
CREATE POLICY {table}_tenant_isolation ON {table}
    USING (org_id = current_setting('qagent.current_org', true)::uuid)
    WITH CHECK (org_id = current_setting('qagent.current_org', true)::uuid);
"""


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)
    logger.info("schema created")


def apply_rls() -> None:
    """Install tenant isolation policies.

    Note that a table owner bypasses RLS unless FORCE is set, which is why the
    policy statement includes it. The application role must not be the table owner
    in a production deployment.
    """
    with engine.begin() as connection:
        for table in TENANT_TABLES:
            connection.execute(text(_POLICY.format(table=table)))
    logger.info("row-level security applied to %d tables", len(TENANT_TABLES))


def init() -> None:
    create_schema()
    apply_rls()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init()
    print(f"schema ready, RLS applied to {len(TENANT_TABLES)} tables")
