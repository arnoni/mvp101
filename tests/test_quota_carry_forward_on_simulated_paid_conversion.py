import pytest

from app.api.auth import _carry_forward_anon_quota_usage
from app.core.keys import KeyBuilder


class _FakeRedis:
    def __init__(self, data=None, ttl_by_key=None):
        self.data = data or {}
        self.ttl_by_key = ttl_by_key or {}

    async def get(self, key):
        return self.data.get(key)

    async def ttl(self, key):
        return self.ttl_by_key.get(key, -1)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttl_by_key[key] = ex


@pytest.mark.asyncio
async def test_carries_forward_anon_usage_to_paid_quota_key():
    anon_id = "anon-1"
    user_id = "user-1"
    anon_key = KeyBuilder.quota_rolling24h("anon", anon_id)
    paid_key = KeyBuilder.quota_rolling24h("paid", user_id)
    redis = _FakeRedis(data={anon_key: "2"}, ttl_by_key={anon_key: 3210})

    carried = await _carry_forward_anon_quota_usage(redis, anon_id=anon_id, user_id=user_id)

    assert carried == 2
    assert redis.data[paid_key] == 2
    assert redis.ttl_by_key[paid_key] == 3210


@pytest.mark.asyncio
async def test_does_not_reduce_existing_paid_usage():
    anon_id = "anon-2"
    user_id = "user-2"
    anon_key = KeyBuilder.quota_rolling24h("anon", anon_id)
    paid_key = KeyBuilder.quota_rolling24h("paid", user_id)
    redis = _FakeRedis(data={anon_key: "2", paid_key: "3"}, ttl_by_key={anon_key: 2000, paid_key: 1000})

    carried = await _carry_forward_anon_quota_usage(redis, anon_id=anon_id, user_id=user_id)

    assert carried == 0
    assert redis.data[paid_key] == "3"
    assert redis.ttl_by_key[paid_key] == 1000
