import json
import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, validate_call
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from upstash_redis.asyncio import Redis

from app.core.keys import KeyBuilder


class TierStatus(str, Enum):
    FREE = "FREE"
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
    def _tier_from_plan(plan_code: Optional[str], duration_days: int) -> TierStatus:
        code = (plan_code or "").lower()
        if duration_days >= 3 or code.startswith("3_day"):
            return TierStatus.PASS_3_DAY
        return TierStatus.PASS_1_DAY

    @staticmethod
    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    async def get_tier(
        user_id: Optional[str],
        redis_cli: Optional[Redis],
        db_engine: Optional[AsyncEngine],
        ttl_seconds: int = 600,
    ) -> EntitlementResult:
        if not user_id:
            return EntitlementResult(tier=TierStatus.FREE, daily_limit=3, is_stale=True)

        key = KeyBuilder.entitlement_status(user_id)
        if redis_cli:
            try:
                data = await redis_cli.get(key)
                if data:
                    payload = json.loads(data)
                    verified_at = int(payload.get("verified_at", 0))
                    now = int(time.time())
                    expires_at = payload.get("expires_at")
                    if expires_at is not None and int(expires_at) < now:
                        await redis_cli.delete(key)
                        return EntitlementResult(
                            tier=TierStatus.FREE,
                            active_plan_code=None,
                            daily_limit=3,
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
            except Exception as e:
                logger.error(f"ENTITLEMENT_REDIS_GET_FAILED: {e}", user_id=user_id)
                # Fall through to DB
                pass

        if not db_engine:
            logger.warning("ENTITLEMENT_DB_ENGINE_MISSING", user_id=user_id)
            return EntitlementResult(tier=TierStatus.FREE, daily_limit=3, is_stale=True)

        try:
            async with db_engine.connect() as conn:
                paid_res = await conn.execute(
                    text(
                        """
                        SELECT up.plan_code, up.expires_at, bp.duration_days, bp.daily_limit
                        FROM user_passes up
                        JOIN billing_plans bp ON bp.code = up.plan_code
                        WHERE up.user_id = :user_id
                          AND up.status = 'active'
                          AND up.expires_at > NOW()
                        ORDER BY up.expires_at DESC
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id},
                )
                paid_row = paid_res.mappings().first()
                if paid_row:
                    tier = EntitlementService._tier_from_plan(
                        str(paid_row["plan_code"]), int(paid_row["duration_days"])
                    )
                    expires_ts = int(paid_row["expires_at"].timestamp()) if paid_row.get("expires_at") else None
                    result = EntitlementResult(
                        tier=tier,
                        active_plan_code=str(paid_row["plan_code"]),
                        daily_limit=int(paid_row["daily_limit"]),
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
                return result
        except Exception as e:
            logger.error(f"ENTITLEMENT_DB_QUERY_FAILED: {e}", user_id=user_id)
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
            logger.debug("ENTITLEMENT_CACHE_REDIS_UNAVAILABLE", user_id=user_id)
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
        print(f"About to call redis.set() for key: {key}")
        import asyncio
        try:
            await asyncio.wait_for(
                redis_cli.set(key, json.dumps(payload), ex=ttl_seconds),
                timeout=10,
            )
            print(f"redis.set() finished for key: {key}")
        except asyncio.TimeoutError:
            logger.error(f"ENTITLEMENT_CACHE_REDIS_TIMEOUT: {key}")
        except Exception as e:
            logger.error(f"ENTITLEMENT_CACHE_REDIS_ERROR: {e}", key=key)
