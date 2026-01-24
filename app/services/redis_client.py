# app/services/redis_client.py
from upstash_redis import Redis
from app.core.config import settings

class RedisClientWrapper:
    def __init__(self):
        url = settings.UPSTASH_REDIS_REST_URL
        token = settings.UPSTASH_REDIS_REST_TOKEN
        
        if url and token:
            self.client = Redis(url=url, token=token)
        else:
            self.client = None

    def get(self, key: str):
        if not self.client: return None
        try:
            return self.client.get(key)
        except Exception as e:
            print(f"Redis Error: {e}")
            return None

    def incr(self, key: str) -> int:
        if not self.client: return 0
        try:
            val = self.client.incr(key)
            return int(val) if val is not None else 0
        except Exception as e:
            print(f"Redis Error: {e}")
            return 0

    def setex(self, key: str, time: int, value: str):
        if not self.client: return None
        try:
            # REST client uses 'ex' arg for expiration
            return self.client.set(key, value, ex=time)
        except Exception as e:
            print(f"Redis Error: {e}")
            return None

redis_client = RedisClientWrapper()
