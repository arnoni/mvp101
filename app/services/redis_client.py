# app/services/redis_client.py
"""Thin async wrapper around redis.asyncio that matches the mock API used in the app.
It provides get, setex, and incr methods used for rate‑limiting and Mapbox counters.
"""
import logging
from redis.asyncio import Redis
from app.core.config import settings

import structlog

logger = structlog.get_logger(__name__)

class RealRedisClient:
    def __init__(self, url: str = settings.UPSTASH_REDIS_URL):
        if not url:
            raise ValueError("UPSTASH_REDIS_URL is not set in the environment")
        # Configure connection pool with explicit limits to prevent EBUSY errors
        self._redis = Redis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=10,  # Limit concurrent connections
            socket_keepalive=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
            health_check_interval=30
        )

    async def get(self, key: str):
        try:
            return await self._redis.get(key)
        except Exception as e:
            logger.error("redis_get_error", key=key, error=str(e))
            return None

    async def setex(self, key: str, ttl: int, value: str):
        try:
            await self._redis.setex(key, ttl, value)
        except Exception as e:
            logger.error("redis_setex_error", key=key, error=str(e))

    async def incr(self, key: str):
        try:
            return await self._redis.incr(key)
        except Exception as e:
            logger.error("redis_incr_error", key=key, error=str(e))
            return 0
