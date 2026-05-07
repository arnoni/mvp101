from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import Response

from app.api import auth


class _MagicLandingResult:
    def __init__(self, *, fetchone_value=None, first_value=None):
        self._fetchone_value = fetchone_value
        self._first_value = first_value

    def fetchone(self):
        return self._fetchone_value

    def first(self):
        return self._first_value


class _MagicLandingConnection:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _statement):
        if not self._results:
            raise AssertionError("Unexpected database execute in magic_landing test")
        return self._results.pop(0)


class _MagicLandingBegin:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _MagicLandingDb:
    def __init__(self, results):
        self._results = results

    def begin(self):
        return _MagicLandingBegin(_MagicLandingConnection(self._results))


class _MagicLandingRedis:
    def __init__(self):
        self.sessions = {}
        self.deleted = []

    async def set(self, key, value, *, ex):
        self.sessions[key] = {"value": value, "ex": ex}
        return True

    async def delete(self, key):
        self.deleted.append(key)
        return 1


def _request():
    return SimpleNamespace(state=SimpleNamespace(ab_cohort="A", anon_id=None), cookies={})


def _service(*, db_results, redis=None):
    return SimpleNamespace(db=_MagicLandingDb(db_results), redis=redis or _MagicLandingRedis())


@pytest.mark.asyncio
async def test_magic_landing_imports_checkout_base_and_does_not_name_error(monkeypatch):
    monkeypatch.setattr(auth.settings, "APP_ORIGIN", "https://dilldrill.com")
    service = SimpleNamespace(db=None, redis=None)

    result = await auth.magic_landing("raw-token", _request(), Response(), service)

    assert auth.resolve_checkout_base("https://dilldrill.com") == "https://dilldrill.com"
    assert result.status_code == 303
    assert result.headers["location"] == "https://dilldrill.com/?error=system_error&code=AUTH_MAGIC_SESSION_CACHE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_magic_landing_success_redirects_to_canonical_domain_not_vercel(monkeypatch):
    monkeypatch.setattr(
        auth.settings,
        "APP_ORIGIN",
        "https://dilldrill.com,https://dilldrill-git-branch-example.vercel.app",
    )
    user_id = uuid4()
    token_row = SimpleNamespace(
        id=uuid4(),
        email="Buyer@Example.com",
        redeemed_at=None,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    user_row = SimpleNamespace(id=user_id, email="buyer@example.com")
    service = _service(
        db_results=[
            _MagicLandingResult(fetchone_value=token_row),
            _MagicLandingResult(),
            _MagicLandingResult(first_value=user_row),
            _MagicLandingResult(fetchone_value=None),
        ]
    )

    result = await auth.magic_landing("raw-token", _request(), Response(), service)

    assert result.status_code == 303
    assert result.headers["location"] == "https://dilldrill.com/?magic_success=1"
    assert "vercel.app" not in result.headers["location"]


@pytest.mark.asyncio
async def test_magic_landing_invalid_or_expired_token_redirects_without_500(monkeypatch):
    monkeypatch.setattr(auth.settings, "APP_ORIGIN", "https://dilldrill.com")
    service = _service(db_results=[_MagicLandingResult(fetchone_value=None)])

    result = await auth.magic_landing("expired-or-invalid", _request(), Response(), service)

    assert result.status_code == 303
    assert result.status_code != 500
    assert result.headers["location"] == "https://dilldrill.com/?error=invalid_link"
