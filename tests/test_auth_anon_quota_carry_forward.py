import asyncio

from app.api.auth import (
    ANON_QUOTA_CARRY_FORWARD_LUA_SCRIPT,
    _carry_forward_anon_quota_usage,
    load_anon_quota_carry_forward_script,
    set_anon_quota_carry_forward_script_sha,
)
from app.core.keys import KeyBuilder


class AtomicCarryForwardRedis:
    def __init__(self):
        self.store: dict[str, int | str] = {}
        self.expirations: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self.script_sha = "anon-carry-forward-sha"
        self.loaded_script: str | None = None

    async def script_load(self, script: str) -> str:
        self.loaded_script = script
        return self.script_sha

    async def evalsha(self, sha: str, keys: list[str], args: list[int]):
        assert sha == self.script_sha
        return await self.eval("", keys, args)

    async def eval(self, script: str, keys: list[str], args: list[int]):
        async with self.lock:
            anon_key, paid_key = keys
            (ttl_seconds,) = args
            anon_raw = self.store.get(anon_key)
            if anon_raw is None:
                return 0
            used_count = int(anon_raw)
            if used_count <= 0:
                return 0
            self.store[paid_key] = str(used_count)
            self.expirations[paid_key] = int(ttl_seconds)
            return 1

    async def get(self, key: str):
        return self.store.get(key)


class EvalOnlyCarryForwardRedis:
    def __init__(self):
        self.store: dict[str, int | str] = {}
        self.expirations: dict[str, int] = {}
        self.lock = asyncio.Lock()

    async def eval(self, script: str, keys: list[str], args: list[int]):
        async with self.lock:
            anon_key, paid_key = keys
            (ttl_seconds,) = args
            anon_raw = self.store.get(anon_key)
            if anon_raw is None:
                return 0
            used_count = int(anon_raw)
            if used_count <= 0:
                return 0
            self.store[paid_key] = str(used_count)
            self.expirations[paid_key] = int(ttl_seconds)
            return 1

    async def get(self, key: str):
        return self.store.get(key)


def test_anon_quota_carry_forward_lua_script_uses_cluster_compatible_keys_and_args():
    assert "KEYS[1]" in ANON_QUOTA_CARRY_FORWARD_LUA_SCRIPT
    assert "KEYS[2]" in ANON_QUOTA_CARRY_FORWARD_LUA_SCRIPT
    assert "ARGV[1]" in ANON_QUOTA_CARRY_FORWARD_LUA_SCRIPT
    assert "redis.call('GET', anon_key)" in ANON_QUOTA_CARRY_FORWARD_LUA_SCRIPT
    assert "redis.call('SET', paid_key, tostring(used_count), 'EX', ttl_seconds)" in ANON_QUOTA_CARRY_FORWARD_LUA_SCRIPT


def test_concurrent_anon_quota_carry_forward_is_atomic():
    async def run():
        redis = AtomicCarryForwardRedis()
        await load_anon_quota_carry_forward_script(redis)

        anon_id = "anon-race"
        user_id = "user-race"
        anon_key = KeyBuilder.quota_rolling24h("anon", anon_id)
        paid_key = KeyBuilder.quota_rolling24h("paid", user_id)
        redis.store[anon_key] = "5"

        await asyncio.gather(
            *(
                _carry_forward_anon_quota_usage(
                    user_id=user_id,
                    anon_id=anon_id,
                    redis_cli=redis,
                    ttl_seconds=86400,
                )
                for _ in range(10)
            )
        )

        assert await redis.get(paid_key) == "5"
        assert redis.expirations[paid_key] == 86400
        assert await redis.get(anon_key) == "5"

    asyncio.run(run())


def test_anon_quota_carry_forward_eval_fallback_without_script_sha():
    async def run():
        set_anon_quota_carry_forward_script_sha(None)
        redis = EvalOnlyCarryForwardRedis()
        anon_id = "anon-eval"
        user_id = "user-eval"
        anon_key = KeyBuilder.quota_rolling24h("anon", anon_id)
        paid_key = KeyBuilder.quota_rolling24h("paid", user_id)
        redis.store[anon_key] = "3"

        await _carry_forward_anon_quota_usage(
            user_id=user_id,
            anon_id=anon_id,
            redis_cli=redis,
            ttl_seconds=120,
        )

        assert await redis.get(paid_key) == "3"
        assert redis.expirations[paid_key] == 120

    asyncio.run(run())
