from redis.asyncio import Redis
from app.core.config import settings
from app.core.keys import KeyBuilder
from datetime import datetime, timedelta

class DemandService:
    """
    Tracks demand (requests) per cell to build heatmaps and prioritize precompute.
    """
    
    def __init__(self, redis: Redis):
        self.redis = redis
        
    async def record_query(self, cell_id: str):
        """
        Increment daily demand counter for a cell.
        """
        if not self.redis:
            return
            
        today = datetime.utcnow().strftime("%Y%m%d")
        key = KeyBuilder.demand_daily(cell_id, today)
        
        # Incr and set retention (e.g., 30 days)
        # We don't need a pipeline for this simple op usually, but safer
        pipeline = self.redis.pipeline()
        pipeline.incr(key)
        pipeline.expire(key, 60 * 60 * 24 * 30) # 30 days
        await pipeline.execute()
        
    async def get_demand_rolling(self, cell_id: str, days: int = 14) -> int:
        """
        Aggregates demand over last N days.
        """
        if not self.redis:
            return 0
            
        keys = []
        base = datetime.utcnow()
        for i in range(days):
            d = (base - timedelta(days=i)).strftime("%Y%m%d")
            keys.append(KeyBuilder.demand_daily(cell_id, d))
            
        # Optimization: Use MGET
        values = await self.redis.mget(keys)
        total = sum(int(v) for v in values if v)
        return total
