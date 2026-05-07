"""add ugc report dedup unique constraint

Revision ID: a7b4c9d8e1f2
Revises: 164bdf86a927
Create Date: 2026-05-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7b4c9d8e1f2"
down_revision: Union[str, Sequence[str], None] = "164bdf86a927"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "ugc_reports",
        sa.Column(
            "day_bucket",
            sa.Text(),
            nullable=True,
            server_default=sa.text("to_char((now() AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD')"),
        ),
    )
    op.execute(
        """
        UPDATE ugc_reports
        SET day_bucket = to_char((created_at AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD')
        WHERE day_bucket IS NULL
        """
    )
    op.alter_column("ugc_reports", "day_bucket", existing_type=sa.Text(), nullable=False)
    op.create_unique_constraint(
        "ugc_reports_dedup_unique",
        "ugc_reports",
        ["content_hash", "geo_cell", "day_bucket"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("ugc_reports_dedup_unique", "ugc_reports", type_="unique")
    op.drop_column("ugc_reports", "day_bucket")
