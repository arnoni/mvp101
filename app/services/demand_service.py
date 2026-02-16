from typing import Optional
from upstash_redis.asyncio import Redis
from app.core.config import settings
from app.core.keys import KeyBuilder
from datetime import datetime, timezone

class DemandService:
    """
    Tracks demand (requests) per cell to build heatmaps and prioritize precompute.
    """
    
    def __init__(self, redis: Optional[Redis]):
        self.redis = redis
        
    async def record_query(self, cell_id: str):
        """
        Increment daily demand counter for a cell.
        """
        if not self.redis:
            return
            
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        key = KeyBuilder.demand_daily(cell_id, today)
        
        # Use direct calls
        _ = await self.redis.incr(key)
        _ = await self.redis.expire(key, 60 * 60 * 24 * 30) # 30 days
        
    async def get_demand_rolling(self, cell_id: str, days: int = 14) -> int:
        """
        Aggregates demand over last N days.
        """
        if not self.redis:
            return 0
            
        keys = []
        base = datetime.now(timezone.utc)
        for i in range(days):
            from datetime import timedelta
            d = (base - timedelta(days=i)).strftime("%Y%m%d")
            keys.append(KeyBuilder.demand_daily(cell_id, d))
            
        # Optimization: Use MGET
        values = await self.redis.mget(*keys)
        total = sum(int(v) for v in values if v)
        return total
