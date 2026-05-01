import json
import logging
import time
from enum import Enum
from typing import Optional

import sentry_sdk
import structlog
from pydantic import BaseModel, ConfigDict, validate_call
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine
from upstash_redis.asyncio import Redis

from app.core.keys import KeyBuilder
from app.models.models import BillingPlan, SimulatedBillingPlan, SimulatedUserPass, UserPass

logger = logging.getLogger(__name__)
struct_logger = structlog.get_logger(__name__)


class TierStatus(str, Enum):
    FREE = "FREE"
    SIMULATED_PAID = "SIMULATED_PAID"
    PASS_1_DAY = "PASS_1_DAY"
    PASS_3_DAY = "PASS_3_DAY"


class EntitlementResult(BaseModel):
    tier: TierStatus
    active_plan_code: Optional[str] = None
    daily_limit: int
    expires_at: Optional[int] = None
    is_stale: bool = False
    raw_data: dict = {}


class EntitlementService:
    @staticmethod
    async def _resolve_free_daily_limit(
        *,
        conn,
        user_id: str,
        identity_kind: Optional[str],
        anon_cohort: Optional[str],
    ) -> int:
        if identity_kind == "anon":
            resolved_cohort = anon_cohort if anon_cohort in {"A", "B"} else "A"
            anon_free_res = await conn.execute(
                text(
                    """
                    SELECT daily_limit
                    FROM free_quotas
                    WHERE cohort = :cohort
                    LIMIT 1
                    """
                ),
                {"cohort": resolved_cohort},
            )
            anon_free_row = anon_free_res.mappings().first()
            return int(anon_free_row["daily_limit"]) if anon_free_row and anon_free_row.get("daily_limit") else 3

        free_res = await conn.execute(
            text(
                """
                SELECT u.ab_cohort, fq.daily_limit
                FROM users u
                LEFT JOIN free_quotas fq ON fq.cohort = u.ab_cohort
                WHERE u.id = :user_id
                LIMIT 1
                """
            ),
            {"user_id": user_id},
        )
        free_row = free_res.mappings().first()
        return int(free_row["daily_limit"]) if free_row and free_row.get("daily_limit") else 3

    @staticmethod
    async def _insert_funnel_event(
        *,
        db_engine: Optional[AsyncEngine],
        user_id: str,
        event_name: str,
        effective_tier: str,
        metadata: dict,
    ) -> None:
        if not db_engine:
            return
        try:
            async with db_engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        INSERT INTO funnel_events (
                            event_name,
                            event_source,
                            event_version,
                            user_id,
                            effective_tier,
                            metadata
                        ) VALUES (
                            :event_name,
                            'entitlement_service',
                            1,
                            :user_id::uuid,
                            :effective_tier,
                            :metadata::jsonb
                        )
                        """
                    ),
                    {
                        "event_name": event_name,
                        "user_id": user_id,
                        "effective_tier": effective_tier,
                        "metadata": json.dumps(metadata),
                    },
                )
        except Exception as exc:
            struct_logger.exception(
                "entitlement_funnel_event_insert_failed",
                user_id=user_id,
                event_name=event_name,
                effective_tier=effective_tier,
                metadata=metadata,
                error=str(exc),
            )
            sentry_sdk.capture_exception(exc)

    @staticmethod
    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    async def get_tier(
        user_id: Optional[str],
        redis_cli: Optional[Redis],
        db_engine: Optional[AsyncEngine],
        ttl_seconds: int = 600,
        identity_kind: Optional[str] = None,
        anon_cohort: Optional[str] = None,
    ) -> EntitlementResult:
        if not user_id:
            return EntitlementResult(tier=TierStatus.FREE, daily_limit=3, is_stale=True)

        key = KeyBuilder.entitlement_status(user_id)
        cached_payload: dict = {}
        if redis_cli:
            try:
                data = await redis_cli.get(key)
                if data:
                    payload = json.loads(data)
                    cached_payload = payload
                    verified_at = int(payload.get("verified_at", 0))
                    now = int(time.time())
                    expires_at = payload.get("expires_at")
                    if expires_at is not None and int(expires_at) < now:
                        await redis_cli.delete(key)
                        free_limit = 3
                        if db_engine:
                            async with db_engine.connect() as conn:
                                free_quota_exists_res = await conn.execute(
                                    text("SELECT to_regclass('public.free_quotas') IS NOT NULL AS exists")
                                )
                                free_quota_exists = bool(free_quota_exists_res.scalar())
                                if free_quota_exists:
                                    free_limit = await EntitlementService._resolve_free_daily_limit(
                                        conn=conn,
                                        user_id=user_id,
                                        identity_kind=identity_kind,
                                        anon_cohort=anon_cohort,
                                    )
                        await EntitlementService._insert_funnel_event(
                            db_engine=db_engine,
                            user_id=user_id,
                            event_name="quota_expired",
                            effective_tier="free",
                            metadata={
                                "prior_tier": payload.get("tier"),
                                "prior_daily_limit": payload.get("daily_limit"),
                                "new_daily_limit": free_limit,
                                "source": "redis_expiration_check",
                            },
                        )
                        if payload.get("tier") == TierStatus.SIMULATED_PAID.value:
                            await EntitlementService._insert_funnel_event(
                                db_engine=db_engine,
                                user_id=user_id,
                                event_name="tier_downgraded_simulated_paid_to_free",
                                effective_tier="free",
                                metadata={
                                    "prior_tier": payload.get("tier"),
                                    "new_tier": "FREE",
                                    "source": "redis_expiration_check",
                                },
                            )
                        await EntitlementService._insert_funnel_event(
                            db_engine=db_engine,
                            user_id=user_id,
                            event_name="quota_reset",
                            effective_tier="free",
                            metadata={
                                "new_daily_limit": free_limit,
                                "source": "redis_expiration_check",
                            },
                        )
                        return EntitlementResult(
                            tier=TierStatus.FREE,
                            active_plan_code=None,
                            daily_limit=free_limit,
                            expires_at=None,
                            is_stale=False,
                            raw_data={},
                        )
                    is_stale = (now - verified_at) > ttl_seconds or verified_at > (now + 60)
                    tier_val = str(payload.get("tier", TierStatus.FREE.value))
                    tier = TierStatus(tier_val) if tier_val in TierStatus._value2member_map_ else TierStatus.FREE
                    return EntitlementResult(
                        tier=tier,
                        active_plan_code=payload.get("active_plan_code"),
                        daily_limit=int(payload.get("daily_limit", 3)),
                        expires_at=expires_at,
                        is_stale=is_stale,
                        raw_data=payload,
                    )
            except Exception as exc:
                logger.error("ENTITLEMENT_REDIS_GET_FAILED user_id=%s err=%s", user_id, exc)
                struct_logger.exception("entitlement_redis_get_failed", user_id=user_id, error=str(exc))
                sentry_sdk.capture_exception(exc)

        if not db_engine:
            logger.warning("ENTITLEMENT_DB_ENGINE_MISSING user_id=%s", user_id)
            return EntitlementResult(tier=TierStatus.FREE, daily_limit=3, is_stale=True)

        try:
            async with db_engine.connect() as conn:
                if identity_kind == "anon":
                    resolved_cohort = anon_cohort if anon_cohort in {"A", "B"} else "A"
                    free_quota_exists_res = await conn.execute(
                        text("SELECT to_regclass('public.free_quotas') IS NOT NULL AS exists")
                    )
                    free_quota_exists = bool(free_quota_exists_res.scalar())
                    if free_quota_exists:
                        anon_free_res = await conn.execute(
                            text(
                                """
                                SELECT daily_limit
                                FROM free_quotas
                                WHERE cohort = :cohort
                                LIMIT 1
                                """
                            ),
                            {"cohort": resolved_cohort},
                        )
                        anon_free_row = anon_free_res.mappings().first()
                        daily_limit = int(anon_free_row["daily_limit"]) if anon_free_row and anon_free_row.get("daily_limit") else 3
                    else:
                        daily_limit = 3
                    return EntitlementResult(
                        tier=TierStatus.FREE,
                        active_plan_code=None,
                        daily_limit=daily_limit,
                        expires_at=None,
                        is_stale=False,
                        raw_data={"cohort": resolved_cohort},
                    )

                simulated_res = await conn.execute(
                    select(
                        SimulatedUserPass.plan_code,
                        SimulatedUserPass.expires_at,
                        SimulatedBillingPlan.daily_limit,
                    )
                    .join(SimulatedBillingPlan, SimulatedBillingPlan.code == SimulatedUserPass.plan_code)
                    .where(
                        SimulatedUserPass.user_id == user_id,
                        SimulatedUserPass.status == "active",
                        SimulatedUserPass.expires_at > func.now(),
                    )
                    .order_by(SimulatedUserPass.expires_at.desc())
                    .limit(1)
                )
                simulated_row = simulated_res.first()
                if simulated_row:
                    expires_ts = int(simulated_row.expires_at.timestamp()) if simulated_row.expires_at else None
                    result = EntitlementResult(
                        tier=TierStatus.SIMULATED_PAID,
                        active_plan_code=str(simulated_row.plan_code),
                        daily_limit=int(simulated_row.daily_limit),
                        expires_at=expires_ts,
                        is_stale=False,
                    )
                    await EntitlementService.cache_entitlement(
                        user_id=user_id,
                        tier=result.tier,
                        redis_cli=redis_cli,
                        active_plan_code=result.active_plan_code,
                        daily_limit=result.daily_limit,
                        expires_at=result.expires_at,
                        ttl_seconds=ttl_seconds,
                    )
                    return result

                paid_res = await conn.execute(
                    select(
                        UserPass.plan_code,
                        UserPass.expires_at,
                        BillingPlan.duration_days,
                        BillingPlan.daily_limit,
                    )
                    .join(BillingPlan, BillingPlan.code == UserPass.plan_code)
                    .where(
                        UserPass.user_id == user_id,
                        UserPass.status == "active",
                        UserPass.expires_at > func.now(),
                    )
                    .order_by(UserPass.expires_at.desc())
                    .limit(1)
                )
                paid_row = paid_res.first()
                if paid_row:
                    expires_ts = int(paid_row.expires_at.timestamp()) if paid_row.expires_at else None
                    tier = TierStatus.PASS_3_DAY if int(paid_row.duration_days or 1) >= 3 else TierStatus.PASS_1_DAY
                    result = EntitlementResult(
                        tier=tier,
                        active_plan_code=str(paid_row.plan_code),
                        daily_limit=int(paid_row.daily_limit),
                        expires_at=expires_ts,
                        is_stale=False,
                    )
                    await EntitlementService.cache_entitlement(
                        user_id=user_id,
                        tier=result.tier,
                        redis_cli=redis_cli,
                        active_plan_code=result.active_plan_code,
                        daily_limit=result.daily_limit,
                        expires_at=result.expires_at,
                        ttl_seconds=ttl_seconds,
                    )
                    return result

                # free_quotas may not exist in newer schema revisions; keep entitlement resolution resilient.
                free_quota_exists_res = await conn.execute(
                    text("SELECT to_regclass('public.free_quotas') IS NOT NULL AS exists")
                )
                free_quota_exists = bool(free_quota_exists_res.scalar())

                if free_quota_exists:
                    free_res = await conn.execute(
                        text(
                            """
                            SELECT u.ab_cohort, fq.daily_limit
                            FROM users u
                            LEFT JOIN free_quotas fq ON fq.cohort = u.ab_cohort
                            WHERE u.id = :user_id
                            LIMIT 1
                            """
                        ),
                        {"user_id": user_id},
                    )
                else:
                    free_res = await conn.execute(
                        text(
                            """
                            SELECT u.ab_cohort, NULL::INTEGER AS daily_limit
                            FROM users u
                            WHERE u.id = :user_id
                            LIMIT 1
                            """
                        ),
                        {"user_id": user_id},
                    )

                free_row = free_res.mappings().first()
                daily_limit = int(free_row["daily_limit"]) if free_row and free_row.get("daily_limit") else 3
                result = EntitlementResult(
                    tier=TierStatus.FREE,
                    active_plan_code=None,
                    daily_limit=daily_limit,
                    expires_at=None,
                    is_stale=False,
                )
                await EntitlementService.cache_entitlement(
                    user_id=user_id,
                    tier=result.tier,
                    redis_cli=redis_cli,
                    active_plan_code=None,
                    daily_limit=result.daily_limit,
                    expires_at=None,
                    ttl_seconds=ttl_seconds,
                )
                cached_tier = str(cached_payload.get("tier", ""))
                if cached_tier == TierStatus.SIMULATED_PAID.value:
                    await EntitlementService._insert_funnel_event(
                        db_engine=db_engine,
                        user_id=user_id,
                        event_name="tier_downgraded_simulated_paid_to_free",
                        effective_tier="free",
                        metadata={
                            "prior_tier": cached_tier,
                            "new_tier": "FREE",
                            "source": "db_entitlement_resolution",
                            "new_daily_limit": daily_limit,
                        },
                    )
                    await EntitlementService._insert_funnel_event(
                        db_engine=db_engine,
                        user_id=user_id,
                        event_name="quota_reset",
                        effective_tier="free",
                        metadata={
                            "new_daily_limit": daily_limit,
                            "source": "db_entitlement_resolution",
                        },
                    )
                return result
        except Exception as exc:
            logger.error("ENTITLEMENT_DB_QUERY_FAILED user_id=%s err=%s", user_id, exc)
            struct_logger.exception("entitlement_db_query_failed", user_id=user_id, error=str(exc))
            sentry_sdk.capture_exception(exc)
            return EntitlementResult(tier=TierStatus.FREE, daily_limit=3, is_stale=True)

    @staticmethod
    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    async def cache_entitlement(
        user_id: str,
        tier: TierStatus,
        redis_cli: Optional[Redis],
        active_plan_code: Optional[str],
        daily_limit: int,
        expires_at: Optional[int],
        ttl_seconds: int = 600,
    ):
        if not user_id:
            logger.error("ENTITLEMENT_CACHE_MISSING_USER_ID")
            return
        if not redis_cli:
            logger.debug("ENTITLEMENT_CACHE_REDIS_UNAVAILABLE user_id=%s", user_id)
            return

        key = KeyBuilder.entitlement_status(user_id)
        payload = {
            "version": 3,
            "tier": tier.value,
            "active_plan_code": active_plan_code,
            "daily_limit": int(daily_limit),
            "expires_at": expires_at,
            "verified_at": int(time.time()),
        }
        try:
            await redis_cli.set(key, json.dumps(payload), ex=ttl_seconds)
        except Exception as exc:
            logger.error("ENTITLEMENT_CACHE_REDIS_ERROR key=%s err=%s", key, exc)
            struct_logger.exception("entitlement_cache_redis_error", key=key, user_id=user_id, error=str(exc))
            sentry_sdk.capture_exception(exc)
