"""environment graphql protected fields

Which GraphQL fields an environment's API requires credentials for. GraphQL
introspection does not expose applied directives, so this declaration is the
only source for GraphQL authentication checks (modules/graphql/introspection.py).
Nullable: null means "not declared", and no GraphQL auth check is generated.

Revision ID: b7d2e41c9a15
Revises: a1f4c7e29b03
Create Date: 2026-09-27 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'b7d2e41c9a15'
down_revision: str | None = 'a1f4c7e29b03'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('environments', sa.Column('graphql_protected', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('environments', 'graphql_protected')
