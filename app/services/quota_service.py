from __future__ import annotations

import hashlib
import uuid
from typing import Literal

import sentry_sdk
import structlog
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

# TODO(Vercel): Ensure structlog is configured with JSONRenderer for Vercel log drain compatibility.
logger = structlog.get_logger()


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
    logger.info(
        "quota_initialization_requested",
        user_id=str(user_id),
        daily_limit=int(daily_limit),
        query_fingerprint=None,
        outcome="requested",
    )
    try:
        existing_result = await db.execute(
            text(
                """
                SELECT remaining_quota
                FROM users
                WHERE id = :user_id
                """
            ),
            {"user_id": user_id},
        )
        existing_row = existing_result.first()
        existing_remaining = existing_row.remaining_quota if existing_row is not None else None
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
            exc = ValueError(f"User not found while initializing quota: {user_id}")
            logger.error(
                "quota_user_not_found",
                user_id=str(user_id),
                daily_limit=int(daily_limit),
                query_fingerprint=None,
                outcome="user_not_found",
                error=str(exc),
            )
            sentry_sdk.capture_exception(exc)
            raise exc
        await db.commit()
        remaining_quota = int(row.remaining_quota)
        if existing_remaining is None:
            logger.info(
                "quota_initialized",
                user_id=str(user_id),
                daily_limit=int(daily_limit),
                query_fingerprint=None,
                remaining_quota=remaining_quota,
                outcome="initialized",
            )
        else:
            logger.info(
                "quota_already_initialized",
                user_id=str(user_id),
                daily_limit=int(daily_limit),
                query_fingerprint=None,
                remaining_quota=remaining_quota,
                outcome="existing_quota_returned",
            )
        return remaining_quota
    except ValueError:
        raise
    except Exception as exc:
        logger.exception(
            "quota_initialization_unexpected_error",
            user_id=str(user_id),
            daily_limit=int(daily_limit),
            query_fingerprint=None,
            outcome="unexpected_error",
            error=str(exc),
        )
        sentry_sdk.capture_exception(exc)
        raise


async def has_construction_query(
    *,
    db: AsyncSession,
    user_id: uuid.UUID,
    query_fingerprint: str,
) -> bool:
    logger.info(
        "construction_query_lookup",
        user_id=str(user_id),
        daily_limit=None,
        query_fingerprint=query_fingerprint,
        outcome="lookup_requested",
    )
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
    exists = result.first() is not None
    logger.info(
        "construction_query_exists" if exists else "construction_query_not_found",
        user_id=str(user_id),
        daily_limit=None,
        query_fingerprint=query_fingerprint,
        outcome="exists" if exists else "not_found",
    )
    return exists


async def consume_construction_credit(
    *,
    db: AsyncSession,
    user_id: uuid.UUID,
    daily_limit: int,
    query_fingerprint: str,
) -> QuotaConsumptionResult:
    """Atomically consume one authenticated construction credit if needed."""
    logger.info(
        "construction_credit_consumption_attempted",
        user_id=str(user_id),
        daily_limit=int(daily_limit),
        query_fingerprint=query_fingerprint,
        outcome="attempted",
    )
    duplicate = await has_construction_query(db=db, user_id=user_id, query_fingerprint=query_fingerprint)
    if duplicate:
        remaining = await get_or_initialize_remaining_quota(db=db, user_id=user_id, daily_limit=daily_limit)
        logger.info(
            "construction_credit_duplicate_detected",
            user_id=str(user_id),
            daily_limit=int(daily_limit),
            query_fingerprint=query_fingerprint,
            remaining_quota=remaining,
            outcome="duplicate_no_charge",
        )
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
            logger.info(
                "quota_null_initializing_from_daily_limit",
                user_id=str(user_id),
                daily_limit=int(daily_limit),
                query_fingerprint=query_fingerprint,
                outcome="null_quota_initialized_before_consumption",
            )
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
            logger.info(
                "quota_insufficient",
                user_id=str(user_id),
                daily_limit=int(daily_limit),
                query_fingerprint=query_fingerprint,
                current_remaining=int(current_remaining),
                outcome="insufficient_quota_no_charge",
            )
            sentry_sdk.capture_message(
                f"Insufficient construction quota for user {user_id} on fingerprint {query_fingerprint}",
                level="warning",
            )
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
            logger.warning(
                "quota_concurrency_error",
                user_id=str(user_id),
                daily_limit=int(daily_limit),
                query_fingerprint=query_fingerprint,
                outcome="quota_decrement_lost_race",
            )
            raise QuotaConcurrencyError("quota_decrement_lost_race")
        await db.commit()
        new_remaining_quota = int(updated_row.remaining_quota)
        logger.info(
            "construction_credit_consumed",
            user_id=str(user_id),
            daily_limit=int(daily_limit),
            query_fingerprint=query_fingerprint,
            new_remaining_quota=new_remaining_quota,
            outcome="credit_consumed",
        )
        return QuotaConsumptionResult(
            consumed=True,
            remaining_quota=new_remaining_quota,
            reason="new_construction_query",
        )
    except IntegrityError as exc:
        await db.rollback()
        logger.warning(
            "construction_query_integrity_error",
            user_id=str(user_id),
            daily_limit=int(daily_limit),
            query_fingerprint=query_fingerprint,
            outcome="integrity_error_duplicate_fallback",
            error=str(exc),
        )
        remaining = await get_or_initialize_remaining_quota(db=db, user_id=user_id, daily_limit=daily_limit)
        return QuotaConsumptionResult(
            consumed=False,
            remaining_quota=remaining,
            reason="duplicate_construction_query_no_charge",
        )
    except QuotaConcurrencyError:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception(
            "construction_credit_unexpected_error",
            user_id=str(user_id),
            daily_limit=int(daily_limit),
            query_fingerprint=query_fingerprint,
            outcome="unexpected_error",
            error=str(exc),
        )
        sentry_sdk.capture_exception(exc)
        raise
