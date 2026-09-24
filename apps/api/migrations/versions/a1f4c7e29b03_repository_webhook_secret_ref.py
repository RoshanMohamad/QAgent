"""repository webhook secret ref

Where a repository's GitHub webhook secret is named, never stored - same rule
as `secret_ref` on this table and on `environments` (modules/secrets/,
ADR-0004). Lets a push to a connected repository queue a scan automatically
once an operator has exposed the API publicly and configured the webhook.

Revision ID: a1f4c7e29b03
Revises: 9c9e12caf320
Create Date: 2026-09-24 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a1f4c7e29b03'
down_revision: str | None = '9c9e12caf320'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'repositories', sa.Column('webhook_secret_ref', sa.String(length=200), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('repositories', 'webhook_secret_ref')
