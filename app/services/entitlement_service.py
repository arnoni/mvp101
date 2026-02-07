import json
import time
from enum import Enum
from typing import Optional, Any
from redis.asyncio import Redis
from pydantic import validate_call

class TierStatus(str, Enum):
    FREE = "FREE"
    PAID = "PAID"

class EntitlementResult:
    def __init__(self, tier: TierStatus, is_stale: bool = False, raw_data: dict = None):
        self.tier = tier
        self.is_stale = is_stale
        self.raw_data = raw_data or {}

class EntitlementService:
    """
    Separates session handling from subscription logic.
    """
    
    @staticmethod
    @validate_call
    async def get_tier(user_id: Optional[str], redis_cli: Optional[Redis], ttl_seconds: int = 300) -> EntitlementResult:
        """
        Determines the tier for a given user ID.
        Checks Redis cache first.
        If verified_at is older than TTL, marks as stale.
        """
        if not user_id:
            return EntitlementResult(TierStatus.FREE)
            
        if not redis_cli:
            # If Redis is down, we can't verify. Treat as stale FREE.
            return EntitlementResult(TierStatus.FREE, is_stale=True)
            
        key = f"entitlement:user:{user_id}"
        
        try:
            data = await redis_cli.get(key)
            if data:
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    # Corrupt cache -> self heal
                    await redis_cli.delete(key)
                    return EntitlementResult(TierStatus.FREE, is_stale=True)
                
                tier_val = payload.get("tier", "FREE")
                try:
                    tier = TierStatus(tier_val)
                except ValueError:
                    tier = TierStatus.FREE
                
                verified_at = payload.get("verified_at")
                
                # Monotonic & Existence Checks
                now = int(time.time())
                is_stale = False
                
                if verified_at is None:
                    is_stale = True
                elif verified_at > (now + 60): # Future timestamp (clock skew > 1 min)
                    is_stale = True
                elif (now - verified_at) > ttl_seconds:
                    is_stale = True
                
                return EntitlementResult(tier, is_stale=is_stale, raw_data=payload)
        except Exception:
            pass
            
        # Cache Miss -> Fallback to Postgres (TODO)
        return EntitlementResult(TierStatus.FREE, is_stale=True)

    @staticmethod
    @validate_call
    async def cache_entitlement(
        user_id: str, 
        tier: TierStatus, 
        redis_cli: Redis, 
        ttl_seconds: int = 300,
        provider: str = "stripe",
        subscription_status: str = "active",
        plan: str = "default",
        period_end: Optional[int] = None
    ):
        """
        Stores the entitlement in Redis with verified_at timestamp and extended metadata.
        """
        if not user_id or not redis_cli:
            return
            
        key = f"entitlement:user:{user_id}"
        payload = {
            "schema_version": 1,
            "tier": tier.value,
            "verified_at": int(time.time()),
            "provider": provider,
            "subscription_status": subscription_status,
            "plan": plan,
            "period_end": period_end
        }
        # Set Redis TTL slightly longer than verification TTL to allow for grace logic if needed,
        # but for now we keep them sync or user provided.
        await redis_cli.set(key, json.dumps(payload), ex=ttl_seconds)
