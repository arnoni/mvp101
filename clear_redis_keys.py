import asyncio
from app.services.redis_client import RealRedisClient
import os

async def clear_keys():
    client = RealRedisClient()
    # Delete possible rate limit key (wildcard) and mapbox counter
    # Since we don't know the IP, we can flush the whole DB for testing.
    await client._redis.flushdb()
    await client._redis.close()

if __name__ == "__main__":
    asyncio.run(clear_keys())
