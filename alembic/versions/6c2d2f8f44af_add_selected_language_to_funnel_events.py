"""add_selected_language_to_funnel_events

Revision ID: 6c2d2f8f44af
Revises: c4f7b2e9d1aa
Create Date: 2026-04-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6c2d2f8f44af'
down_revision: Union[str, Sequence[str], None] = 'c4f7b2e9d1aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'funnel_events',
        sa.Column('selected_language', sa.Text(), server_default=sa.text("'en'"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('funnel_events', 'selected_language')
