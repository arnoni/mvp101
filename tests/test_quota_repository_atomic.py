import asyncio

from app.services.quota_repository import QUOTA_EXCEEDED_SENTINEL, QuotaRepository


class AtomicEvalRedis:
    def __init__(self):
        self.store: dict[str, int | str] = {}
        self.expirations: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self.script_sha = "sha-test"

    async def script_load(self, script: str) -> str:
        self.script = script
        return self.script_sha

    async def evalsha(self, sha: str, keys: list[str], args: list[int]):
        assert sha == self.script_sha
        return await self.eval("", keys, args)

    async def eval(self, script: str, keys: list[str], args: list[int]):
        async with self.lock:
            quota_key, idempotency_key = keys
            limit, quota_ttl, idempotency_ttl = args
            current = int(self.store.get(quota_key, 0) or 0)
            if idempotency_key and idempotency_key in self.store:
                return [0, current, max(0, limit - current)]
            if current >= limit:
                return [QUOTA_EXCEEDED_SENTINEL, current, 0]
            new_usage = current + 1
            self.store[quota_key] = new_usage
            if quota_key not in self.expirations:
                self.expirations[quota_key] = quota_ttl
            if idempotency_key:
                self.store[idempotency_key] = "1"
                self.expirations[idempotency_key] = idempotency_ttl
            return [1, new_usage, max(0, limit - new_usage)]

    async def get(self, key: str):
        return self.store.get(key)


def test_atomic_quota_allows_only_limit_under_concurrency():
    async def run():
        redis = AtomicEvalRedis()
        repo = QuotaRepository(redis)
        await repo.load_lua_scripts()

        limit = 10
        results = await asyncio.gather(
            *(repo.check_and_consume("dd:quota:anon:race:rolling24h", limit) for _ in range(50))
        )

        allowed = [item for item in results if item[0]]
        blocked = [item for item in results if not item[0]]

        assert len(allowed) == limit
        assert len(blocked) == 40
        assert int(await redis.get("dd:quota:anon:race:rolling24h")) == limit
        assert redis.expirations["dd:quota:anon:race:rolling24h"] == 86400

    asyncio.run(run())


def test_idempotency_key_does_not_double_consume():
    async def run():
        redis = AtomicEvalRedis()
        repo = QuotaRepository(redis)

        first = await repo.check_and_consume(
            "dd:quota:paid:user_1:rolling24h",
            3,
            idempotency_key="idem:one",
        )
        replay = await repo.check_and_consume(
            "dd:quota:paid:user_1:rolling24h",
            3,
            idempotency_key="idem:one",
        )

        assert first == (True, 2)
        assert replay == (True, 2)
        assert int(await redis.get("dd:quota:paid:user_1:rolling24h")) == 1

    asyncio.run(run())
