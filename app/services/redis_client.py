# app/services/redis_client.py
from typing import Any
from upstash_redis.asyncio import Redis as UpstashRedis
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class RedisClientWrapper:
    def __init__(self):
        url = settings.UPSTASH_REDIS_REST_URL
        token = settings.UPSTASH_REDIS_REST_TOKEN
        
        if url and token:
            self.client: UpstashRedis | None = UpstashRedis(url=url, token=token)
            logger.info("Upstash Redis (REST) client initialized.")
        else:
            self.client = None
            logger.warning("Upstash Redis credentials missing. Redis client disabled.")

    async def get(self, key: str) -> str | None:
        if not self.client: return None
        try:
            val = await self.client.get(key)
            return str(val) if val is not None else None
        except Exception as e:
            logger.error(f"Redis Client GET Error: {e} for key: {key}")
            return None

    async def incr(self, key: str) -> int:
        if not self.client: return 0
        try:
            val = await self.client.incr(key)
            return int(val)
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

    async def eval(self, script: str, keys: list[str] | None = None, args: list[Any] | None = None):
        if not self.client: return None
        try:
            return await self.client.eval(script, keys, args)
        except Exception as e:
            logger.error(f"Redis Client EVAL Error: {e}")
            return None

redis_client = RedisClientWrapper()
