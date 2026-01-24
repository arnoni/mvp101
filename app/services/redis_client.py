import os
from upstash_redis import Redis

class RedisClientWrapper:
    def __init__(self):
        url = os.environ.get("UPSTASH_REDIS_REST_URL")
        token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
        
        if url and token:
            self.client = Redis(url=url, token=token)
        else:
            self.client = None

    def get(self, key):
        if not self.client: return None
        try:
            return self.client.get(key)
        except Exception as e:
            print(f"Redis Error: {e}")
            return None

    def incr(self, key):
        if not self.client: return None
        try:
            return self.client.incr(key)
        except Exception as e:
            print(f"Redis Error: {e}")
            return None

    def setex(self, key, time, value):
        if not self.client: return None
        try:
            # REST client uses 'ex' arg for expiration
            return self.client.set(key, value, ex=time)
        except Exception as e:
            print(f"Redis Error: {e}")
            return None

redis_client = RedisClientWrapper()
