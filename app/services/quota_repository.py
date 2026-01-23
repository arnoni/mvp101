import logging
from typing import Optional, Protocol, Any

import structlog

logger = structlog.get_logger(__name__)

class RedisInterface(Protocol):
    async def get(self, key: str) -> Any: ...
    async def setex(self, key: str, ttl: int, value: str) -> Any: ...
    async def incr(self, key: str) -> int: ...

class InMemoryFallbackStore:
    """Simple in-memory store for fallback when Redis is down."""
    def __init__(self):
        self._store = {}
    
    async def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    async def incr(self, key: str) -> int:
        val = self._store.get(key, 0)
        new_val = int(val) + 1
        self._store[key] = str(new_val)
        return new_val
    
    async def setex(self, key: str, ttl: int, value: str):
        # TTL ignored in fallback
        self._store[key] = value

class QuotaRepository:
    """
    Repository for quota management with Redis backing and in-memory fallback.
    """
    def __init__(self, redis_client: Optional[RedisInterface] = None):
        self.redis_client = redis_client
        self.fallback_store = InMemoryFallbackStore()

    async def get_usage(self, key: str) -> int:
        try:
            if self.redis_client:
                val = await self.redis_client.get(key)
                return int(val) if val else 0
            return int(val) if val else 0
        except Exception as e:
            logger.error("quota_get_usage_error", error=str(e), key=key, fallback=True)
        
        # Fallback
        val = await self.fallback_store.get(key)
        return int(val) if val else 0

    async def increment(self, key: str, ttl: int = 86400) -> int:
        try:
            if self.redis_client:
                # Basic INCR logic. 
                # Note: This logic assumes key existence or doesn't care about setting TTL on first incr for this MVP snippet.
                val = await self.redis_client.incr(key)
                return val
                val = await self.redis_client.incr(key)
                return val
        except Exception as e:
             logger.error("quota_increment_error", error=str(e), key=key, fallback=True)

        return await self.fallback_store.incr(key)

    async def check_available(self, key: str, max_limit: int) -> bool:
        usage = await self.get_usage(key)
        return usage < max_limit
