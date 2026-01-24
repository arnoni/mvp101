# clear_redis_keys.py
from app.services.redis_client import redis_client

def clear_keys():
    if redis_client.client:
        try:
            redis_client.client.flushdb()
            print("All keys cleared (FlushDB).")
        except Exception as e:
            print(f"Error: {e}")
    else:
        print("Redis disabled.")

if __name__ == "__main__":
    clear_keys()
