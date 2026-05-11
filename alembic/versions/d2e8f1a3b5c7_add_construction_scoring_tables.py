"""Add cell_poi_stats and cell_poi_percentiles for construction scoring.

The pois table is NOT modified here — it already has
geom GEOGRAPHY(POINT,4326) with GiST index idx_pois_geom.

Revision ID: d2e8f1a3b5c7
Revises: b8d2f4c6a9e1
Create Date: 2026-05-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d2e8f1a3b5c7"
down_revision: Union[str, Sequence[str], None] = "b8d2f4c6a9e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cell_poi_stats",
        sa.Column("cell_id", sa.Text(), nullable=False),
        sa.Column("grid_poi_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("cell_id", name="pk_cell_poi_stats"),
    )
    op.create_index("ix_cell_poi_stats_cell_id", "cell_poi_stats", ["cell_id"])

    op.create_table(
        "cell_poi_percentiles",
        sa.Column("percentile", sa.Float(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("sample_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("percentile", name="pk_cell_poi_percentiles"),
    )
    op.create_index(
        "ix_cell_poi_percentiles_pct", "cell_poi_percentiles", ["percentile"]
    )

    # NOTE: Run CLUSTER pois USING idx_pois_geom manually after data is
    # loaded. This physically reorders rows by spatial proximity, reducing
    # random I/O during GiST index scans. Safe to skip while the pois table is
    # empty or small.


def downgrade() -> None:
    op.drop_index("ix_cell_poi_percentiles_pct", table_name="cell_poi_percentiles")
    op.drop_table("cell_poi_percentiles")
    op.drop_index("ix_cell_poi_stats_cell_id", table_name="cell_poi_stats")
    op.drop_table("cell_poi_stats")
