# clear_redis.py
from app.services.redis_client import redis_client

def clear():
    if redis_client.client:
        # Upstash REST client uses different command names sometimes, but 'flushdb' is standard
        # However, it depends on the library. upstash-redis has 'flushdb'
        try:
            redis_client.client.flushdb()
            print("Redis DB cleared.")
        except Exception as e:
            print(f"Error clearing Redis: {e}")
    else:
        print("Redis client not initialized.")

if __name__ == "__main__":
    clear()
