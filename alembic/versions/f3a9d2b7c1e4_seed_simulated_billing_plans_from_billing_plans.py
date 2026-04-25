"""seed_simulated_billing_plans_from_billing_plans

Revision ID: f3a9d2b7c1e4
Revises: e1a2c3d4f5b6
Create Date: 2026-04-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f3a9d2b7c1e4"
down_revision: Union[str, Sequence[str], None] = "e1a2c3d4f5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO simulated_billing_plans (
            code,
            display_name,
            duration_hours,
            daily_limit,
            cohort,
            is_active
        )
        SELECT
            bp.code,
            bp.display_name,
            (bp.duration_days * 24) AS duration_hours,
            bp.daily_limit,
            CASE
                WHEN bp.code ILIKE '%_a' THEN 'A'
                WHEN bp.code ILIKE '%_b' THEN 'B'
                ELSE NULL
            END AS cohort,
            bp.is_active
        FROM billing_plans bp
        ON CONFLICT (code) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            duration_hours = EXCLUDED.duration_hours,
            daily_limit = EXCLUDED.daily_limit,
            cohort = EXCLUDED.cohort,
            is_active = EXCLUDED.is_active,
            updated_at = NOW();
        """
    )


def downgrade() -> None:
    # Intentionally no-op: this migration is a one-time seed snapshot for MVP.
    pass
