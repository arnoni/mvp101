import json
import time
from enum import Enum
from typing import Optional, Any
from upstash_redis.asyncio import Redis
from pydantic import validate_call, ConfigDict, BaseModel
from app.core.keys import KeyBuilder

class TierStatus(str, Enum):
    FREE = "FREE"
    PAID = "PAID"

class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    UNPAID = "unpaid"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    PAUSED = "paused"
    UNKNOWN = "unknown"

class EntitlementResult(BaseModel):
    tier: TierStatus
    status: SubscriptionStatus
    is_stale: bool = False
    expires_at: Optional[int] = None
    raw_data: dict = {}

class EntitlementService:
    """
    Authorization Logic.
    Mapping:
      - active, trialing -> PAID
      - everything else -> FREE
    """

    @staticmethod
    def map_status_to_tier(status_val: str) -> TierStatus:
        s = status_val.lower()
        if s in [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIALING.value]:
            return TierStatus.PAID
        return TierStatus.FREE

    @staticmethod
    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    async def get_tier(user_id: Optional[str], redis_cli: Optional[Redis], ttl_seconds: int = 600) -> EntitlementResult:
        if not user_id:
            return EntitlementResult(tier=TierStatus.FREE, status=SubscriptionStatus.UNKNOWN)
            
        if not redis_cli:
            # Fail closed-ish (Free tier) if Redis down
            return EntitlementResult(tier=TierStatus.FREE, status=SubscriptionStatus.UNKNOWN, is_stale=True)
            
        key = KeyBuilder.entitlement_status(user_id)
        
        try:
            data = await redis_cli.get(key)
            if data:
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    await redis_cli.delete(key)
                    return EntitlementResult(tier=TierStatus.FREE, status=SubscriptionStatus.UNKNOWN, is_stale=True)
                
                # Parse
                sub_status_str = payload.get("subscription_status", "unknown")
                verified_at = payload.get("verified_at", 0)
                
                # Map
                tier = EntitlementService.map_status_to_tier(sub_status_str)
                
                # Check Stale
                now = int(time.time())
                is_stale = False
                if (now - verified_at) > ttl_seconds:
                    is_stale = True
                if verified_at > (now + 60): # Future clock skew
                    is_stale = True
                    
                return EntitlementResult(
                    tier=tier, 
                    status=SubscriptionStatus(sub_status_str) if sub_status_str in SubscriptionStatus._value2member_map_ else SubscriptionStatus.UNKNOWN,
                    is_stale=is_stale,
                    raw_data=payload
                )
        except Exception:
            pass
            
        # Cache Miss
        return EntitlementResult(tier=TierStatus.FREE, status=SubscriptionStatus.UNKNOWN, is_stale=True)

    @staticmethod
    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    async def cache_entitlement(
        user_id: str, 
        status: str,
        redis_cli: Redis, 
        ttl_seconds: int = 600,
        provider: str = "stripe",
        plan: str = "default",
        period_end: Optional[int] = None
    ):
        if not user_id or not redis_cli:
            return
            
        key = KeyBuilder.entitlement_status(user_id)
        payload = {
            "version": 2,
            "subscription_status": status,
            "verified_at": int(time.time()),
            "provider": provider,
            "plan": plan,
            "period_end": period_end
        }
        await redis_cli.set(key, json.dumps(payload), ex=ttl_seconds)

