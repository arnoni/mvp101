"""merge join research counter and request ip heads

Revision ID: 164bdf86a927
Revises: 1c9a6b2f77aa, 3d7e9a1b2c44
Create Date: 2026-05-02 11:45:10.493228

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '164bdf86a927'
down_revision: Union[str, Sequence[str], None] = ('1c9a6b2f77aa', '3d7e9a1b2c44')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
