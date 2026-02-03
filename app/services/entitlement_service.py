import json
import time
from enum import Enum
from typing import Optional, Any
from redis.asyncio import Redis

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
    async def get_tier(user_id: Optional[str], redis_cli: Optional[Redis], ttl_seconds: int = 300) -> EntitlementResult:
        """
        Determines the tier for a given user ID.
        Checks Redis cache first.
        If verified_at is older than TTL, marks as stale.
        
        Args:
            user_id: The user's ID.
            redis_cli: Async Redis client.
            ttl_seconds: Max age of verification before considered stale.
            
        Returns:
            EntitlementResult: Contains tier and stale status.
        """
        if not user_id:
            return EntitlementResult(TierStatus.FREE)
            
        if not redis_cli:
            # If Redis is down, we can't verify. Treat as stale FREE? 
            # Or just FREE? User said: "If miss and route is paid required, fail closed."
            # We'll return FREE with is_stale=True to indicate we couldn't verify.
            return EntitlementResult(TierStatus.FREE, is_stale=True)
            
        key = f"entitlement:user:{user_id}"
        
        try:
            data = await redis_cli.get(key)
            if data:
                payload = json.loads(data)
                tier = TierStatus(payload.get("tier", "FREE"))
                verified_at = payload.get("verified_at", 0)
                
                # Check for staleness
                now = int(time.time())
                is_stale = (now - verified_at) > ttl_seconds
                
                return EntitlementResult(tier, is_stale=is_stale, raw_data=payload)
        except Exception:
            pass
            
        # Cache Miss -> Fallback to Postgres (TODO)
        # For now, return FREE and stale because we haven't implemented Postgres yet
        # Once Postgres is implemented, this would fetch from DB and return non-stale result
        return EntitlementResult(TierStatus.FREE, is_stale=True)

    @staticmethod
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
            "tier": tier.value,
            "verified_at": int(time.time()),
            "provider": provider,
            "subscription_status": subscription_status,
            "plan": plan,
            "period_end": period_end
        }
        await redis_cli.set(key, json.dumps(payload), ex=ttl_seconds)
