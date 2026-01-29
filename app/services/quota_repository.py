import logging
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

from redis.asyncio import Redis

class QuotaRepository:
    """
    Redis-backed quota repository with fail-closed behavior (no in-memory fallback).
    """
    def __init__(self, redis_client: Optional[Redis] = None):
        self.redis_client: Optional[Redis] = redis_client

    async def get_usage(self, key: str) -> int:
        if not self.redis_client:
            raise RuntimeError("redis_unavailable")
        try:
            val = await self.redis_client.get(key)
            if val is None:
                return 0
            try:
                return int(val)
            except Exception:
                logger.error("quota_parse_error", key=key, raw_value=str(val))
                return 0
        except Exception as e:
            logger.error("quota_get_usage_error", error=str(e), key=key)
            raise RuntimeError("redis_unavailable")

    async def increment(self, key: str, ttl: int = 86400) -> int:
        if not self.redis_client:
            raise RuntimeError("redis_unavailable")
        try:
            val = await self.redis_client.incr(key)
            if val == 1:
                await self.redis_client.expire(key, ttl)
            return int(val) if val is not None else 1
        except Exception as e:
            logger.error("quota_increment_error", error=str(e), key=key)
            raise RuntimeError("redis_unavailable")

    async def check_available(self, key: str, max_limit: int) -> bool:
        usage = await self.get_usage(key)
        return usage < max_limit

    async def check_and_consume(self, key: str, daily_limit: int, ttl: int = 86400) -> tuple[bool, int]:
        """
        Atomically check and consume one unit, returning (allowed, remaining).
        Uses a Lua script to ensure racing requests cannot exceed the limit.
        """
        if not self.redis_client:
            raise RuntimeError("redis_unavailable")
        script = """
        local key = KEYS[1]
        local limit = tonumber(ARGV[1])
        local ttl = tonumber(ARGV[2])
        local current = redis.call('GET', key)
        if current == false then
          redis.call('SET', key, 1, 'EX', ttl)
          return {1, limit - 1}
        end
        local count = tonumber(current)
        if count >= limit then
          return {0, 0}
        end
        count = redis.call('INCR', key)
        return {1, limit - count}
        """
        try:
            result = await self.redis_client.eval(script, 1, key, daily_limit, ttl)
            allowed = bool(result[0] == 1)
            remaining = int(result[1])
            return allowed, remaining
        except Exception as e:
            logger.error("quota_lua_error", error=str(e), key=key)
            raise RuntimeError("redis_unavailable")
