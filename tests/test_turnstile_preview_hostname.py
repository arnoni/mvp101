import pytest
from fastapi import HTTPException

from app.utils.security import verify_turnstile, settings


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

    def _fake_client(*args, **kwargs):
        return _FakeAsyncClient(
            *args,
            payload={"success": True, "hostname": "demo-arnonis-projects.vercel.app"},
            **kwargs,
        )

    monkeypatch.setattr("app.utils.security.httpx.AsyncClient", _fake_client)

    assert await verify_turnstile("token") is True
