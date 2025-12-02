import asyncio
from app.services.redis_client import RealRedisClient

async def clear():
    client = RealRedisClient()
    await client._redis.flushdb()
    await client._redis.close()

if __name__ == "__main__":
    asyncio.run(clear())
