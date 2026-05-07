import pytest

from app.api.auth import _carry_forward_anon_quota_usage
from app.core.keys import KeyBuilder


class _FakeRedis:
    def __init__(self, data=None, ttl_by_key=None):
        self.data = data or {}
        self.ttl_by_key = ttl_by_key or {}

    async def eval(self, script, keys, args):
        anon_key, paid_key = keys
        (ttl_seconds,) = args
        anon_raw = self.data.get(anon_key)
        if anon_raw is None:
            return 0
        used_count = int(anon_raw)
        if used_count <= 0:
            return 0
        self.data[paid_key] = str(used_count)
        self.ttl_by_key[paid_key] = int(ttl_seconds)
        return 1


@pytest.mark.asyncio
async def test_carries_forward_anon_usage_to_paid_quota_key():
    anon_id = "anon-1"
    user_id = "user-1"
    anon_key = KeyBuilder.quota_rolling24h("anon", anon_id)
    paid_key = KeyBuilder.quota_rolling24h("paid", user_id)
    redis = _FakeRedis(data={anon_key: "2"})

    await _carry_forward_anon_quota_usage(
        user_id=user_id,
        anon_id=anon_id,
        redis_cli=redis,
        ttl_seconds=3210,
    )

    assert redis.data[paid_key] == "2"
    assert redis.ttl_by_key[paid_key] == 3210
    assert redis.data[anon_key] == "2"


@pytest.mark.asyncio
async def test_overwrites_existing_paid_usage_to_match_original_carry_forward_behavior():
    anon_id = "anon-2"
    user_id = "user-2"
    anon_key = KeyBuilder.quota_rolling24h("anon", anon_id)
    paid_key = KeyBuilder.quota_rolling24h("paid", user_id)
    redis = _FakeRedis(data={anon_key: "2", paid_key: "3"}, ttl_by_key={paid_key: 1000})

    await _carry_forward_anon_quota_usage(
        user_id=user_id,
        anon_id=anon_id,
        redis_cli=redis,
        ttl_seconds=2000,
    )

    assert redis.data[paid_key] == "2"
    assert redis.ttl_by_key[paid_key] == 2000
