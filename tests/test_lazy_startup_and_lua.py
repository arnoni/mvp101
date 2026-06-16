import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from app.api import auth as auth_module
from app.core.keys import KeyBuilder
from app.services import quota_repository as quota_module
from app.services.quota_repository import QuotaRepository


class _ExplodingAsyncLock:
    async def __aenter__(self):
        raise AssertionError("hot path must not acquire script load lock")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self):
        self.connect_calls = 0
        self.dispose_calls = 0

    def connect(self):
        self.connect_calls += 1
        raise AssertionError("startup must not connect to database")

    async def dispose(self):
        self.dispose_calls += 1


class _FakeRedis:
    def __init__(self, *, url: str, token: str):
        self.url = url
        self.token = token
        self.close_calls = 0

    async def ping(self):
        raise AssertionError("startup must not ping Redis")

    async def script_load(self, _script: str):
        raise AssertionError("startup must not load Redis scripts")

    async def close(self):
        self.close_calls += 1


@pytest.fixture(autouse=True)
def _reset_lua_sha_state():
    quota_module._quota_sha = None
    auth_module.set_anon_quota_carry_forward_script_sha(None)
    yield
    quota_module._quota_sha = None
    auth_module.set_anon_quota_carry_forward_script_sha(None)


def test_lifespan_constructs_clients_without_neon_or_upstash_network(monkeypatch):
    async def run():
        from app import main as main_module

        fake_engine = _FakeEngine()
        monkeypatch.setattr(main_module.settings, "ENV", "development")
        monkeypatch.setattr(main_module.settings, "DATABASE_URL", "postgresql://user:pass@db.example/db")
        monkeypatch.setattr(main_module.settings, "ENABLE_REDIS", True)
        monkeypatch.setattr(main_module.settings, "UPSTASH_REDIS_REST_URL", "https://redis.example")
        monkeypatch.setattr(main_module.settings, "UPSTASH_REDIS_REST_TOKEN", "token")
        monkeypatch.setattr(main_module, "create_asyncpg_engine", lambda *_args, **_kwargs: fake_engine)
        monkeypatch.setattr(main_module, "Redis", _FakeRedis)

        app = FastAPI()
        async with main_module.lifespan(app):
            assert app.state.db_engine is fake_engine
            assert isinstance(app.state.redis, _FakeRedis)
            assert isinstance(app.state.quota_repo, QuotaRepository)

        assert fake_engine.connect_calls == 0
        assert fake_engine.dispose_calls == 1
        assert app.state.redis.close_calls == 1

    asyncio.run(run())


def test_build_async_engine_constructs_without_connecting(monkeypatch):
    from app import main as main_module

    fake_engine = _FakeEngine()
    monkeypatch.setattr(main_module.settings, "DATABASE_URL", "postgresql://user:pass@db.example/db")
    monkeypatch.setattr(main_module, "create_asyncpg_engine", lambda *_args, **_kwargs: fake_engine)

    assert main_module.build_async_engine() is fake_engine
    assert fake_engine.connect_calls == 0


def test_quota_existing_sha_hot_path_evalsha_without_lock(monkeypatch):
    async def run():
        redis = SimpleNamespace(
            evalsha=AsyncMock(return_value=[1, 1, 4]),
            script_load=AsyncMock(side_effect=AssertionError("script_load must not run")),
        )
        repo = QuotaRepository(redis, quota_script_sha="sha-known")
        monkeypatch.setattr(quota_module, "_quota_script_lock", _ExplodingAsyncLock())

        result = await repo.consume_quota("dd:quota:anon:a:rolling24h", 5)

        assert result == (1, 1, 4)
        redis.evalsha.assert_awaited_once_with("sha-known", ["dd:quota:anon:a:rolling24h", ""], [5, 86400, 300])
        redis.script_load.assert_not_awaited()

    asyncio.run(run())


def test_quota_noscript_loads_once_and_retries_original_operation():
    async def run():
        redis = SimpleNamespace(
            evalsha=AsyncMock(side_effect=[RuntimeError("NOSCRIPT missing"), [1, 1, 4]]),
            script_load=AsyncMock(return_value="sha-new"),
        )
        repo = QuotaRepository(redis, quota_script_sha="sha-old")

        result = await repo.consume_quota("dd:quota:anon:a:rolling24h", 5)

        assert result == (1, 1, 4)
        redis.script_load.assert_awaited_once()
        assert redis.evalsha.await_args_list[0].args == (
            "sha-old",
            ["dd:quota:anon:a:rolling24h", ""],
            [5, 86400, 300],
        )
        assert redis.evalsha.await_args_list[1].args == (
            "sha-new",
            ["dd:quota:anon:a:rolling24h", ""],
            [5, 86400, 300],
        )

    asyncio.run(run())


def test_quota_failed_lazy_script_load_re_raises_and_does_not_grant():
    async def run():
        redis_error = RuntimeError("redis unavailable")
        redis = SimpleNamespace(
            evalsha=AsyncMock(side_effect=RuntimeError("NOSCRIPT missing")),
            script_load=AsyncMock(side_effect=redis_error),
            get=AsyncMock(),
            set=AsyncMock(),
            incr=AsyncMock(),
        )
        repo = QuotaRepository(redis, quota_script_sha="sha-old")

        with pytest.raises(RuntimeError, match="redis unavailable"):
            await repo.check_and_consume("dd:quota:anon:a:rolling24h", 5)

        redis.script_load.assert_awaited_once()
        redis.get.assert_not_called()
        redis.set.assert_not_called()
        redis.incr.assert_not_called()

    asyncio.run(run())


def test_quota_operation_invokes_lua_not_get_set_sequence():
    async def run():
        redis = SimpleNamespace(
            evalsha=AsyncMock(return_value=[1, 1, 4]),
            eval=AsyncMock(),
            get=AsyncMock(),
            set=AsyncMock(),
            incr=AsyncMock(),
        )
        repo = QuotaRepository(redis, quota_script_sha="sha-known")

        await repo.check_and_consume("dd:quota:anon:a:rolling24h", 5)

        redis.evalsha.assert_awaited_once()
        redis.eval.assert_not_called()
        redis.get.assert_not_called()
        redis.set.assert_not_called()
        redis.incr.assert_not_called()

    asyncio.run(run())


def test_quota_concurrent_first_use_loads_script_once():
    async def run():
        script_load_calls = 0

        async def script_load(_script: str) -> str:
            nonlocal script_load_calls
            script_load_calls += 1
            await asyncio.sleep(0)
            return "sha-new"

        redis = SimpleNamespace(
            evalsha=AsyncMock(return_value=[1, 1, 4]),
            script_load=AsyncMock(side_effect=script_load),
        )
        repo = QuotaRepository(redis)

        await asyncio.gather(
            repo.check_and_consume("dd:quota:anon:a:rolling24h", 5),
            repo.check_and_consume("dd:quota:anon:b:rolling24h", 5),
        )

        assert script_load_calls == 1
        assert redis.evalsha.await_count == 2

    asyncio.run(run())


def test_anon_carry_forward_noscript_is_independent_from_quota_script():
    async def run():
        quota_module._quota_sha = "quota-sha-stays-put"
        redis = SimpleNamespace(
            evalsha=AsyncMock(side_effect=[RuntimeError("NOSCRIPT missing"), 1]),
            script_load=AsyncMock(return_value="anon-sha-new"),
        )
        auth_module.set_anon_quota_carry_forward_script_sha("anon-sha-old")

        result = await auth_module._carry_forward_anon_quota_usage(
            user_id="user-a",
            anon_id="anon-a",
            redis_cli=redis,
        )

        assert result == 1
        assert quota_module._quota_sha == "quota-sha-stays-put"
        redis.script_load.assert_awaited_once()
        anon_key = KeyBuilder.quota_rolling24h("anon", "anon-a")
        paid_key = KeyBuilder.quota_rolling24h("paid", "user-a")
        assert redis.evalsha.await_args_list[1].args == ("anon-sha-new", [anon_key, paid_key], [86400])

    asyncio.run(run())


def test_anon_carry_forward_concurrent_first_use_loads_script_once():
    async def run():
        script_load_calls = 0

        async def script_load(_script: str) -> str:
            nonlocal script_load_calls
            script_load_calls += 1
            await asyncio.sleep(0)
            return "anon-sha-new"

        redis = SimpleNamespace(
            evalsha=AsyncMock(return_value=1),
            script_load=AsyncMock(side_effect=script_load),
        )

        await asyncio.gather(
            auth_module._carry_forward_anon_quota_usage(
                user_id="user-a",
                anon_id="anon-a",
                redis_cli=redis,
            ),
            auth_module._carry_forward_anon_quota_usage(
                user_id="user-b",
                anon_id="anon-b",
                redis_cli=redis,
            ),
        )

        assert script_load_calls == 1
        assert redis.evalsha.await_count == 2

    asyncio.run(run())
