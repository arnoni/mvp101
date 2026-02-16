# app/services/redis_client.py
from upstash_redis.asyncio import Redis
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class RedisClientWrapper:
    def __init__(self):
        url = settings.UPSTASH_REDIS_REST_URL
        token = settings.UPSTASH_REDIS_REST_TOKEN
        
        if url and token:
            self.client = Redis(url=url, token=token)
            logger.info("Upstash Redis (REST) client initialized.")
        else:
            self.client = None
            logger.warning("Upstash Redis credentials missing. Redis client disabled.")

    async def get(self, key: str):
        if not self.client: return None
        try:
            val = await self.client.get(key)
            return val
        except Exception as e:
            logger.error(f"Redis Client GET Error: {e} for key: {key}")
            return None

    async def incr(self, key: str) -> int:
        if not self.client: return 0
        try:
            val = await self.client.incr(key)
            return int(val) if val is not None else 0
        except Exception as e:
            logger.error(f"Redis Client INCR Error: {e} for key: {key}")
            return 0

    async def setex(self, key: str, time: int, value: str):
        if not self.client: return None
        try:
            return await self.client.set(key, value, ex=time)
        except Exception as e:
            logger.error(f"Redis Client SETEX Error: {e} for key: {key}")
            return None

    async def eval(self, script: str, numkeys: int, *keys_and_args):
        if not self.client: return None
        try:
            return await self.client.eval(script, numkeys, *keys_and_args)
        except Exception as e:
            logger.error(f"Redis Client EVAL Error: {e}")
            return None

redis_client = RedisClientWrapper()
