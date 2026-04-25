"""add_input_format_stats_daily

Revision ID: e1a2c3d4f5b6
Revises: 6c2d2f8f44af, 9f6c1a7d2b11
Create Date: 2026-04-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e1a2c3d4f5b6"
down_revision: Union[str, Sequence[str], None] = ("6c2d2f8f44af", "9f6c1a7d2b11")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS input_format_stats_daily (
            stat_date DATE NOT NULL,
            target_mode TEXT NOT NULL,
            input_format TEXT NOT NULL,
            input_parse_status TEXT NOT NULL,
            input_host TEXT NOT NULL DEFAULT 'none',
            user_state TEXT NOT NULL DEFAULT 'unknown',
            count INTEGER NOT NULL DEFAULT 0,
            first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (
                stat_date,
                target_mode,
                input_format,
                input_parse_status,
                input_host,
                user_state
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS input_format_stats_daily")
