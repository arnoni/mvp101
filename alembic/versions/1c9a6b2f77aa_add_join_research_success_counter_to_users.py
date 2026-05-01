"""add join research aggregated success counter to users

Revision ID: 1c9a6b2f77aa
Revises: f3a9d2b7c1e4
Create Date: 2026-05-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "1c9a6b2f77aa"
down_revision = "f3a9d2b7c1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("join_research_aggregated_success_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "join_research_aggregated_success_count")
