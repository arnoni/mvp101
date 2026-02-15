import time
from redis.asyncio import Redis
from app.core.config import settings
from app.core.keys import KeyBuilder

class AnomalyService:
    """
    detects abuse patterns:
    1. Velocity: Too many distinct cells queried in short time? (Maybe not distinct cells, just requests)
       Actually, quota handles request count.
       Anomaly might track 'sweep' behavior - querying across large area.
       Plan says:
       - Velocity: track requests per minute (short bursts).
       - Sweep: track unique cells visited.
    """
    
    def __init__(self, redis: Redis):
        self.redis = redis
        
    async def record_action(self, identity_kind: str, identity_id: str, cell_id: str = None):
        """
        Records a user action for anomaly detection.
        """
        if not self.redis:
            return

        pipeline = self.redis.pipeline()
        
        # 1. Velocity (Requests/10min)
        vel_key = KeyBuilder.anomaly_velocity(identity_kind, identity_id)
        pipeline.incr(vel_key)
        pipeline.expire(vel_key, settings.ABUSE_VELOCITY_TTL_SECONDS)
        
        # 2. Sweep (Unique Cells/30min)
        if cell_id:
            sweep_key = KeyBuilder.anomaly_sweep(identity_kind, identity_id)
            pipeline.pfadd(sweep_key, cell_id)
            pipeline.expire(sweep_key, settings.ABUSE_SWEEP_TTL_SECONDS)
            
        await pipeline.execute()

    async def check_is_abusive(self, identity_kind: str, identity_id: str) -> bool:
        """
        Returns True if user exceeds anomaly thresholds.
        """
        if not self.redis:
            return False
            
        vel_key = KeyBuilder.anomaly_velocity(identity_kind, identity_id)
        sweep_key = KeyBuilder.anomaly_sweep(identity_kind, identity_id)
        
        # We can implement a Lua script for strictness, but 2 GETs is fine for MVP102
        try:
            vel_count, unique_cells = await self.redis.mget(vel_key, sweep_key)
            # pfcount for sweep?
            # Wait, mget returns the string value. HyperLogLog (PFADD) needs PFCOUNT.
            # So MGET is wrong for sweep_key if using HLL.
            # But we used keys.py which says 'sweep'.
            pass
        except Exception:
            return False

        # Correct approach using pipeline
        pipeline = self.redis.pipeline()
        pipeline.get(vel_key)
        pipeline.pfcount(sweep_key)
        results = await pipeline.execute()
        
        vel_val = int(results[0] or 0)
        sweep_val = int(results[1] or 0)
        
        # Thresholds
        # If Velocity > 100 in 10m? (High burst)
        # If Sweep > 50 unique cells in 30m? (Data scraping)
        
        # We need thresholds in Settings.
        # Let's assume some defaults if not in settings.
        VELOCITY_LIMIT = 60 # 1 per 10 secons avg? No, 60 requests in 10 mins = 6/min.
        # Quota is strict (e.g. 50/day).
        # So Anomaly is for detecting bots bumping against limits or creating new IDs?
        # This is per INTERFACE ID.
        
        # If quota is 2/day for free, velocity of 60 is impossible unless they bypass quota.
        # But for PAID (50/day), maybe they do it all in 1 second.
        
        # Let's say Abuse = > 20 requests in 1 minute?
        # The TTL is 600s (10m).
        # So 20 in 10m is low.
        # Let's stick to: if usage matches 80% of daily quota in 1 minute -> Spike.
        
        # For MVP102, just return False unless simple logic:
        if vel_val > 100: 
            return True
        if sweep_val > 20:
            return True
            
        return False
