# app/services/redis_client.py
"""Thin async wrapper around redis.asyncio that matches the mock API used in the app.
It provides get, setex, and incr methods used for rate‑limiting and Mapbox counters.
"""
import logging
from redis.asyncio import Redis
from app.core.config import settings

logger = logging.getLogger(__name__)

class RealRedisClient:
    def __init__(self, url: str = settings.UPSTASH_REDIS_URL):
        if not url:
            raise ValueError("UPSTASH_REDIS_URL is not set in the environment")
        self._redis = Redis.from_url(url)

    async def get(self, key: str):
        try:
            return await self._redis.get(key)
        except Exception as e:
            logger.error(f"Redis GET error for {key}: {e}")
            return None

    async def setex(self, key: str, ttl: int, value: str):
        try:
            await self._redis.setex(key, ttl, value)
        except Exception as e:
            logger.error(f"Redis SETEX error for {key}: {e}")

    async def incr(self, key: str):
        try:
            return await self._redis.incr(key)
        except Exception as e:
            logger.error(f"Redis INCR error for {key}: {e}")
            return 0
