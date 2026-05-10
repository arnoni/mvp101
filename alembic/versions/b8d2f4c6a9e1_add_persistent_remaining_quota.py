"""add persistent remaining quota

Revision ID: b8d2f4c6a9e1
Revises: a7b4c9d8e1f2
Create Date: 2026-05-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b8d2f4c6a9e1"
down_revision: Union[str, Sequence[str], None] = "a7b4c9d8e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("remaining_quota", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_users_remaining_quota_non_negative",
        "users",
        "remaining_quota IS NULL OR remaining_quota >= 0",
    )
    op.create_table(
        "construction_queries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="construction_queries_user_id_fkey"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "fingerprint", name="uq_construction_queries_user_fingerprint"),
    )
    op.create_index("ix_construction_queries_user_id", "construction_queries", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_construction_queries_user_id", table_name="construction_queries")
    op.drop_table("construction_queries")
    op.drop_constraint("ck_users_remaining_quota_non_negative", "users", type_="check")
    op.drop_column("users", "remaining_quota")
