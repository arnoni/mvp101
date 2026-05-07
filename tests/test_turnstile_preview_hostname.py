import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.utils.security import verify_turnstile, settings, protect_mutation


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self._payload = kwargs.pop("payload")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *_args, **_kwargs):
        return _FakeResponse(self._payload)


@pytest.mark.asyncio
async def test_preview_turnstile_rejects_non_vercel_hostname(monkeypatch):
    monkeypatch.setattr("app.utils.security.redis_client", None)
    monkeypatch.setattr(settings, "ENV", "preview")
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "test-secret")

    def _fake_client(*args, **kwargs):
        return _FakeAsyncClient(
            *args,
            payload={"success": True, "hostname": "malicious.example.com"},
            **kwargs,
        )

    monkeypatch.setattr("app.utils.security.httpx.AsyncClient", _fake_client)

    with pytest.raises(HTTPException) as exc:
        await verify_turnstile("token")

    assert exc.value.status_code == 403
    assert exc.value.detail == "Invalid Turnstile hostname"


@pytest.mark.asyncio
async def test_preview_turnstile_allows_expected_vercel_hostname(monkeypatch):
    monkeypatch.setattr("app.utils.security.redis_client", None)
    monkeypatch.setattr(settings, "ENV", "preview")
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "test-secret")

    def _fake_client(*args, **kwargs):
        return _FakeAsyncClient(
            *args,
            payload={"success": True, "hostname": "demo-arnonis-projects.vercel.app"},
            **kwargs,
        )

    monkeypatch.setattr("app.utils.security.httpx.AsyncClient", _fake_client)

    assert await verify_turnstile("token") is True


def _mutation_request(origin: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/search",
            "headers": [
                (b"content-type", b"application/json"),
                (b"origin", origin.encode("utf-8")),
            ],
        }
    )


@pytest.mark.asyncio
async def test_protect_mutation_allows_preview_vercel_origin(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "preview")
    monkeypatch.setattr(settings, "APP_ORIGIN", "https://dilldrill.com,https://www.dilldrill.com")

    req = _mutation_request("https://mvp101-cxqcks22g-arnonis-projects.vercel.app")
    assert await protect_mutation(req) is True


@pytest.mark.asyncio
async def test_protect_mutation_rejects_non_allowed_origin_outside_preview_suffix(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "preview")
    monkeypatch.setattr(settings, "APP_ORIGIN", "https://dilldrill.com,https://www.dilldrill.com")

    req = _mutation_request("https://malicious.example.com")
    with pytest.raises(HTTPException) as exc:
        await protect_mutation(req)

    assert exc.value.status_code == 403
    assert exc.value.detail == "origin not allowed"


class _SequenceAsyncClient:
    payloads = []
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data=None):
        self.__class__.requests.append({"url": url, "data": data})
        return _FakeResponse(self.__class__.payloads.pop(0))


class _MemoryRedis:
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def setex(self, key, time, value):
        self.values[key] = value
        return True


@pytest.mark.asyncio
async def test_turnstile_valid_and_invalid_tokens(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "development")
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "test-secret")
    monkeypatch.setattr("app.utils.security.redis_client", None)
    _SequenceAsyncClient.payloads = [{"success": True}, {"success": False, "error-codes": ["invalid-input-response"]}]
    _SequenceAsyncClient.requests = []
    monkeypatch.setattr("app.utils.security.httpx.AsyncClient", _SequenceAsyncClient)

    assert await verify_turnstile("valid-token", client_ip="203.0.113.10", anon_id="anon-a") is True
    assert await verify_turnstile("invalid-token", client_ip="203.0.113.10", anon_id="anon-b") is False
    assert _SequenceAsyncClient.requests[0]["data"]["secret"] == "test-secret"
    assert _SequenceAsyncClient.requests[0]["data"]["response"] == "valid-token"
    assert _SequenceAsyncClient.requests[0]["data"]["remoteip"] == "203.0.113.10"


@pytest.mark.asyncio
async def test_turnstile_cache_is_not_shared_for_same_nat_ip(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "development")
    monkeypatch.setattr(settings, "CLOUDFLARE_TURNSTILE_SECRET", "test-secret")
    monkeypatch.setattr("app.utils.security.redis_client", _MemoryRedis())
    _SequenceAsyncClient.payloads = [{"success": True}, {"success": False, "error-codes": ["invalid-input-response"]}]
    _SequenceAsyncClient.requests = []
    monkeypatch.setattr("app.utils.security.httpx.AsyncClient", _SequenceAsyncClient)

    assert await verify_turnstile("valid-token", client_ip="198.51.100.5", anon_id="anon-a") is True
    assert await verify_turnstile("invalid-token", client_ip="198.51.100.5", anon_id="anon-b") is False
    assert len(_SequenceAsyncClient.requests) == 2


@pytest.mark.asyncio
async def test_lifespan_rejects_smoke_turnstile_token_in_production(monkeypatch):
    from fastapi import FastAPI
    from app.main import lifespan

    monkeypatch.setattr("app.main.settings.ENV", "production")
    monkeypatch.setattr("app.main.settings.SMOKE_TURNSTILE_TOKEN", "test123")

    with pytest.raises(RuntimeError, match="SMOKE_TURNSTILE_TOKEN must not be set in production"):
        async with lifespan(FastAPI()):
            pass
