"""add_missing_raw_sql_tables

Revision ID: c4f7b2e9d1aa
Revises: bf88275af98e
Create Date: 2026-04-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c4f7b2e9d1aa'
down_revision: Union[str, Sequence[str], None] = 'bf88275af98e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'cell_poi_precompute',
        sa.Column('cell_id', sa.Text(), nullable=False),
        sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('cell_id'),
    )
    op.create_table(
        'free_quotas',
        sa.Column('cohort', sa.Text(), nullable=False),
        sa.Column('daily_limit', sa.Integer(), nullable=False),
        sa.CheckConstraint('daily_limit > 0', name='free_quotas_daily_limit_check'),
        sa.PrimaryKeyConstraint('cohort'),
    )
    op.create_table(
        'webhook_events',
        sa.Column('provider', sa.Text(), nullable=False),
        sa.Column('event_id', sa.Text(), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status', sa.Text(), server_default=sa.text("'received'"), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("status IN ('received', 'processed', 'failed')", name='webhook_events_status_check'),
        sa.PrimaryKeyConstraint('provider', 'event_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('webhook_events')
    op.drop_table('free_quotas')
    op.drop_table('cell_poi_precompute')
