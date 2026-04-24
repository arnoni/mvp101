"""seed_quota_and_billing_plan_data

Revision ID: 9f6c1a7d2b11
Revises: c4f7b2e9d1aa
Create Date: 2026-04-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '9f6c1a7d2b11'
down_revision: Union[str, Sequence[str], None] = 'c4f7b2e9d1aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO free_quotas (cohort, daily_limit) VALUES ('A', 3), ('B', 4)
        ON CONFLICT (cohort) DO UPDATE SET daily_limit = EXCLUDED.daily_limit;
        """
    )

    op.execute(
        """
        INSERT INTO billing_plans (code, display_name, display_price, amount_usd_cents, duration_days, daily_limit) VALUES
        ('1_day_test_a', '1 Day Test A', '$1.00', 100, 1, 10),
        ('1_day_test_b', '1 Day Test B', '$1.00', 100, 1, 8),
        ('3_day_test_a', '3 Day Test A', '$3.00', 300, 3, 15),
        ('3_day_test_b', '3 Day Test B', '$3.00', 300, 3, 12)
        ON CONFLICT (code) DO UPDATE SET display_name = EXCLUDED.display_name, display_price = EXCLUDED.display_price, amount_usd_cents = EXCLUDED.amount_usd_cents, duration_days = EXCLUDED.duration_days, daily_limit = EXCLUDED.daily_limit;
        """
    )


def downgrade() -> None:
    # Intentionally no-op: seeded baseline data should remain present.
    pass
