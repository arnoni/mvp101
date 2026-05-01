"""add_request_ip_to_magic_link_tokens

Revision ID: 3d7e9a1b2c44
Revises: 6c2d2f8f44af, 9f6c1a7d2b11
Create Date: 2026-05-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d7e9a1b2c44'
down_revision: Union[str, Sequence[str], None] = ('6c2d2f8f44af', '9f6c1a7d2b11')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('magic_link_tokens', sa.Column('request_ip', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('magic_link_tokens', 'request_ip')
