"""Retention policy helpers for future scheduled cleanup jobs.

This module intentionally does not schedule or trigger cleanup. It only defines
SQL helpers that can be called by a future cron/background runner.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, delete, func, or_, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.models import MagicLinkToken, SimulatedPaymentIntent, SimulatedUserPass

MAGIC_LINK_RETENTION_DAYS = 30
SIMULATED_INTENT_RETENTION_DAYS = 180
SIMULATED_PASS_RETENTION_DAYS = 365


@dataclass
class RetentionResult:
    affected_rows: int


class RetentionService:
    """SQL retention operations for magic links and simulated billing artifacts."""

    @staticmethod
    async def purge_magic_links(db_engine: AsyncEngine) -> RetentionResult:
        """Delete old magic links.

        Logic:
        - (redeemed_at IS NOT NULL AND redeemed_at < now() - 30 days)
          OR
        - (expires_at < now() - 30 days)
        """
        stmt = delete(MagicLinkToken).where(
            or_(
                and_(
                    MagicLinkToken.redeemed_at.is_not(None),
                    MagicLinkToken.redeemed_at < (func.now() - text(f"INTERVAL '{MAGIC_LINK_RETENTION_DAYS} days'")),
                ),
                MagicLinkToken.expires_at < (func.now() - text(f"INTERVAL '{MAGIC_LINK_RETENTION_DAYS} days'")),
            )
        )
        async with db_engine.begin() as conn:
            result = await conn.execute(stmt)
            return RetentionResult(affected_rows=int(result.rowcount or 0))

    @staticmethod
    async def purge_simulated_intents(db_engine: AsyncEngine) -> RetentionResult:
        """Delete simulated intents older than 180 days by created_at."""
        stmt = delete(SimulatedPaymentIntent).where(
            SimulatedPaymentIntent.created_at < (func.now() - text(f"INTERVAL '{SIMULATED_INTENT_RETENTION_DAYS} days'"))
        )
        async with db_engine.begin() as conn:
            result = await conn.execute(stmt)
            return RetentionResult(affected_rows=int(result.rowcount or 0))

    @staticmethod
    async def rollover_expired_simulated_passes(db_engine: AsyncEngine) -> RetentionResult:
        """Mark active simulated passes as expired when expires_at is in the past."""
        stmt = (
            update(SimulatedUserPass)
            .where(
                SimulatedUserPass.status == "active",
                SimulatedUserPass.expires_at < func.now(),
            )
            .values(status="expired", updated_at=func.now())
        )
        async with db_engine.begin() as conn:
            result = await conn.execute(stmt)
            return RetentionResult(affected_rows=int(result.rowcount or 0))

    @staticmethod
    async def purge_simulated_passes(db_engine: AsyncEngine) -> RetentionResult:
        """Delete aged non-active simulated passes.

        Logic:
        - status IN ('expired', 'revoked')
        - updated_at < now() - 365 days
        """
        stmt = delete(SimulatedUserPass).where(
            SimulatedUserPass.status.in_(["expired", "revoked"]),
            SimulatedUserPass.updated_at < (func.now() - text(f"INTERVAL '{SIMULATED_PASS_RETENTION_DAYS} days'")),
        )
        async with db_engine.begin() as conn:
            result = await conn.execute(stmt)
            return RetentionResult(affected_rows=int(result.rowcount or 0))
