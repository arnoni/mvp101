import time
from typing import Optional
from upstash_redis.asyncio import Redis
from app.core.config import settings
from app.core.keys import KeyBuilder

class AnomalyService:
    """
    detects abuse patterns:
    1. Velocity: tracks requests per minute (short bursts).
    2. Sweep: tracks unique cells visited (data scraping).
    """
    
    def __init__(self, redis: Optional[Redis]):
        self.redis = redis
        
    async def record_action(self, identity_kind: str, identity_id: str, cell_id: Optional[str] = None):
        """
        Records a user action for anomaly detection.
        """
        if not self.redis:
            return

        # Use direct calls (REST is one HTTP op per call anyway)
        # 1. Velocity (Requests/10min)
        vel_key = KeyBuilder.anomaly_velocity(identity_kind, identity_id)
        _ = await self.redis.incr(vel_key)
        _ = await self.redis.expire(vel_key, settings.ABUSE_VELOCITY_TTL_SECONDS)
        
        # 2. Sweep (Unique Cells/30min)
        if cell_id:
            sweep_key = KeyBuilder.anomaly_sweep(identity_kind, identity_id)
            _ = await self.redis.pfadd(sweep_key, cell_id)
            _ = await self.redis.expire(sweep_key, settings.ABUSE_SWEEP_TTL_SECONDS)

    async def check_is_abusive(self, identity_kind: str, identity_id: str) -> bool:
        """
        Returns True if user exceeds anomaly thresholds.
        """
        if not self.redis:
            return False
            
        vel_key = KeyBuilder.anomaly_velocity(identity_kind, identity_id)
        sweep_key = KeyBuilder.anomaly_sweep(identity_kind, identity_id)
        
        try:
            # Get velocity directly
            vel_val = await self.redis.get(vel_key)
            vel_val = int(vel_val or 0)
            
            # Get sweep count directly (HyperLogLog)
            sweep_val = await self.redis.pfcount(sweep_key)
            sweep_val = int(sweep_val or 0)
            
            # Thresholds
            if vel_val > 100: 
                return True
            if sweep_val > 20:
                return True
                
        except Exception:
            return False
            
        return False
