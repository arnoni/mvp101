from upstash_redis.asyncio import Redis
from app.core.config import settings
from app.core.keys import KeyBuilder

class ReputationService:
    """
    Tracks IP reputation score.
    0 = Neutral
    >0 = Suspicious/Bad
    <0 = Trusted
    """
    
    def __init__(self, redis: Redis):
        self.redis = redis
        
    async def get_score(self, ip: str) -> int:
        if not self.redis or not ip:
            return 0
        key = KeyBuilder.reputation(ip)
        val = await self.redis.get(key)
        return int(val) if val else 0
        
    async def report_action(self, ip: str, score_delta: int):
        """
        Adjusts reputation. Positive delta -> More suspicious.
        """
        if not self.redis or not ip:
            return
            
        key = KeyBuilder.reputation(ip)
        # Just incrby and expire directly (REST compatible)
        await self.redis.incrby(key, score_delta)
        await self.redis.expire(key, 60 * 60 * 24 * 7) # 7 days retention
