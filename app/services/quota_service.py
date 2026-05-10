from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


QuotaReason = Literal[
    "new_construction_query",
    "demand_report_no_charge",
    "duplicate_construction_query_no_charge",
    "insufficient_quota",
]


class QuotaConsumptionResult(BaseModel):
    consumed: bool
    remaining_quota: int
    reason: QuotaReason


class QuotaConcurrencyError(RuntimeError):
    """Raised when an atomic quota decrement unexpectedly loses a race."""


# Anonymous quota storage decision:
# Anonymous users do not have a users row before authentication, so they remain
# Redis-only via QuotaRepository. This service is intentionally only called for
# authenticated users; magic-link redemption initializes users.remaining_quota
# from the carried-forward Redis usage when the identity transitions to a user.
# Daily resets for registered free users are intentionally out of scope for
# this migration and require a follow-on scheduled-task ticket with a
# users.last_quota_reset_at column. Until that lands, free registered users are
# initialized once and then decremented durably.


def compute_construction_fingerprint(lat: float, lon: float, radius_m: int) -> str:
    """Return the durable, per-query construction fingerprint."""
    key = f"construction:{round(lat, 5)}:{round(lon, 5)}:{radius_m}"
    return hashlib.sha256(key.encode()).hexdigest()


async def get_or_initialize_remaining_quota(
    *,
    db: AsyncSession,
    user_id: uuid.UUID,
    daily_limit: int,
) -> int:
    """
    Return current remaining_quota for an authenticated user.

    If users.remaining_quota is NULL, initialize it from the caller-supplied
    daily_limit. The daily limit is resolved by entitlement middleware; this
    service does not duplicate EntitlementService logic.
    """
    result = await db.execute(
        text(
            """
            UPDATE users
            SET remaining_quota = CASE
                WHEN remaining_quota IS NULL THEN :daily_limit
                ELSE remaining_quota
            END,
            updated_at = CASE
                WHEN remaining_quota IS NULL THEN now()
                ELSE updated_at
            END
            WHERE id = :user_id
            RETURNING remaining_quota
            """
        ),
        {"user_id": user_id, "daily_limit": int(daily_limit)},
    )
    row = result.first()
    if row is None:
        raise ValueError(f"User not found while initializing quota: {user_id}")
    await db.commit()
    return int(row.remaining_quota)


async def has_construction_query(
    *,
    db: AsyncSession,
    user_id: uuid.UUID,
    query_fingerprint: str,
) -> bool:
    result = await db.execute(
        text(
            """
            SELECT 1
            FROM construction_queries
            WHERE user_id = :user_id AND fingerprint = :fingerprint
            LIMIT 1
            """
        ),
        {"user_id": user_id, "fingerprint": query_fingerprint},
    )
    return result.first() is not None


async def consume_construction_credit(
    *,
    db: AsyncSession,
    user_id: uuid.UUID,
    daily_limit: int,
    query_fingerprint: str,
) -> QuotaConsumptionResult:
    """Atomically consume one authenticated construction credit if needed."""
    duplicate = await has_construction_query(db=db, user_id=user_id, query_fingerprint=query_fingerprint)
    if duplicate:
        remaining = await get_or_initialize_remaining_quota(db=db, user_id=user_id, daily_limit=daily_limit)
        return QuotaConsumptionResult(
            consumed=False,
            remaining_quota=remaining,
            reason="duplicate_construction_query_no_charge",
        )

    try:
        user_result = await db.execute(
            text(
                """
                SELECT remaining_quota
                FROM users
                WHERE id = :user_id
                FOR UPDATE
                """
            ),
            {"user_id": user_id},
        )
        user_row = user_result.first()
        if user_row is None:
            raise ValueError(f"User not found while consuming quota: {user_id}")

        current_remaining = user_row.remaining_quota
        if current_remaining is None:
            current_remaining = int(daily_limit)
            await db.execute(
                text(
                    """
                    UPDATE users
                    SET remaining_quota = :daily_limit,
                        updated_at = now()
                    WHERE id = :user_id
                    """
                ),
                {"user_id": user_id, "daily_limit": current_remaining},
            )

        if int(current_remaining) <= 0:
            await db.commit()
            return QuotaConsumptionResult(
                consumed=False,
                remaining_quota=0,
                reason="insufficient_quota",
            )

        await db.execute(
            text(
                """
                INSERT INTO construction_queries (user_id, fingerprint)
                VALUES (:user_id, :fingerprint)
                """
            ),
            {"user_id": user_id, "fingerprint": query_fingerprint},
        )
        update_result = await db.execute(
            text(
                """
                UPDATE users
                SET remaining_quota = remaining_quota - 1,
                    updated_at = now()
                WHERE id = :user_id AND remaining_quota > 0
                RETURNING remaining_quota
                """
            ),
            {"user_id": user_id},
        )
        updated_row = update_result.first()
        if updated_row is None:
            raise QuotaConcurrencyError("quota_decrement_lost_race")
        await db.commit()
        return QuotaConsumptionResult(
            consumed=True,
            remaining_quota=int(updated_row.remaining_quota),
            reason="new_construction_query",
        )
    except IntegrityError:
        await db.rollback()
        remaining = await get_or_initialize_remaining_quota(db=db, user_id=user_id, daily_limit=daily_limit)
        return QuotaConsumptionResult(
            consumed=False,
            remaining_quota=remaining,
            reason="duplicate_construction_query_no_charge",
        )
    except Exception:
        await db.rollback()
        raise
