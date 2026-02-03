import json
import time
from enum import Enum
from typing import Optional, Any
from redis.asyncio import Redis

class TierStatus(str, Enum):
    FREE = "FREE"
    PAID = "PAID"

class EntitlementService:
    """
    Separates session handling from subscription logic.
    """
    
    @staticmethod
    async def get_tier(user_id: Optional[str], redis_cli: Optional[Redis]) -> TierStatus:
        """
        Determines the tier for a given user ID.
        Checks Redis cache first, then falls back to (future) Postgres.
        
        Args:
            user_id: The user's ID.
            redis_cli: Async Redis client.
            
        Returns:
            TierStatus: FREE or PAID.
        """
        if not user_id:
            return TierStatus.FREE
            
        if not redis_cli:
            # Fallback if Redis is down/missing: default to FREE for safety
            return TierStatus.FREE
            
        key = f"entitlement:user:{user_id}"
        
        try:
            data = await redis_cli.get(key)
            if data:
                payload = json.loads(data)
                return TierStatus(payload.get("tier", "FREE"))
        except Exception:
            pass
            
        # Cache Miss -> Fallback to Postgres (TODO)
        # For now, default to FREE
        return TierStatus.FREE

    @staticmethod
    async def cache_entitlement(user_id: str, tier: TierStatus, redis_cli: Redis, ttl_seconds: int = 300):
        """
        Stores the entitlement in Redis with verified_at timestamp.
        """
        if not user_id or not redis_cli:
            return
            
        key = f"entitlement:user:{user_id}"
        payload = {
            "tier": tier.value,
            "verified_at": int(time.time())
        }
        await redis_cli.set(key, json.dumps(payload), ex=ttl_seconds)
