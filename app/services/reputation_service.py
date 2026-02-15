from redis.asyncio import Redis
from app.core.config import settings
from app.core.keys import KeyBuilder

class ReputationService:
    """
    Tracks IP reputation score.
    High score = Good? Or High score = Bad?
    Let's use:
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
        # Use incrby
        # pipeline to set expire if new?
        # Just incr and expire
        async with self.redis.pipeline() as pipe:
            pipe.incrby(key, score_delta)
            pipe.expire(key, 60 * 60 * 24 * 7) # 7 days retention
            await pipe.execute()
