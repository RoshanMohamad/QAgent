"""browser sessions, recorded actions, ai analysis

The three CLAUDE.md section 20 tables that did not exist yet. Recordings from
the browser extension were only ever files on the developer's disk
(`qagent record-import`); they now belong to a project. And the Project
Analyst's result was only kept as the latest `projects.stack`, overwritten on
every analysis; `ai_analysis` keeps each one.

All three carry org_id and are in models.TENANT_TABLES, so db_init applies
row-level security to them after this upgrade, like every other tenant table.

Revision ID: c3e8f1a4d207
Revises: b7d2e41c9a15
Create Date: 2026-09-29 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c3e8f1a4d207'
down_revision: str | None = 'b7d2e41c9a15'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    ]


def upgrade() -> None:
    op.create_table('browser_sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('start_url', sa.String(length=2000), nullable=True),
    sa.Column('created_by', sa.String(length=320), nullable=True),
    sa.Column('summary', sa.JSON(), nullable=False),
    sa.Column('ui_flow', sa.JSON(), nullable=False),
    sa.Column('api_cases', sa.JSON(), nullable=False),
    sa.Column('diagnostics', sa.JSON(), nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_browser_sessions_org_id'), 'browser_sessions', ['org_id'], unique=False)
    op.create_index(op.f('ix_browser_sessions_project_id'), 'browser_sessions', ['project_id'], unique=False)

    op.create_table('recorded_actions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('session_id', sa.UUID(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=32), nullable=False),
    sa.Column('selector', sa.String(length=1000), nullable=True),
    sa.Column('value', sa.Text(), nullable=True),
    sa.Column('url', sa.String(length=2000), nullable=True),
    sa.Column('tag', sa.String(length=64), nullable=True),
    sa.Column('text', sa.String(length=300), nullable=True),
    sa.Column('redacted', sa.Boolean(), nullable=False),
    sa.Column('fragile', sa.Boolean(), nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['session_id'], ['browser_sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_recorded_actions_org_id'), 'recorded_actions', ['org_id'], unique=False)
    op.create_index(op.f('ix_recorded_actions_selector'), 'recorded_actions', ['selector'], unique=False)
    op.create_index('ix_recorded_actions_session_seq', 'recorded_actions', ['session_id', 'sequence'], unique=False)

    op.create_table('ai_analysis',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('org_id', sa.UUID(), nullable=False),
    sa.Column('project_id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=True),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('agent', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=500), nullable=True),
    sa.Column('commit_sha', sa.String(length=64), nullable=True),
    sa.Column('content', sa.JSON(), nullable=False),
    *_timestamps(),
    sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['run_id'], ['test_runs.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ai_analysis_org_id'), 'ai_analysis', ['org_id'], unique=False)
    op.create_index('ix_ai_analysis_project_kind', 'ai_analysis', ['project_id', 'kind', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_ai_analysis_project_kind', table_name='ai_analysis')
    op.drop_index(op.f('ix_ai_analysis_org_id'), table_name='ai_analysis')
    op.drop_table('ai_analysis')
    op.drop_index('ix_recorded_actions_session_seq', table_name='recorded_actions')
    op.drop_index(op.f('ix_recorded_actions_selector'), table_name='recorded_actions')
    op.drop_index(op.f('ix_recorded_actions_org_id'), table_name='recorded_actions')
    op.drop_table('recorded_actions')
    op.drop_index(op.f('ix_browser_sessions_project_id'), table_name='browser_sessions')
    op.drop_index(op.f('ix_browser_sessions_org_id'), table_name='browser_sessions')
    op.drop_table('browser_sessions')
