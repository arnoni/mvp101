from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.search import SearchResponse
from app.services.entitlement_service import EntitlementResult, TierStatus


class FakeQuotaRepo:
    def __init__(self, usage: int = 0, consume_allowed: bool = True, fail_consume: bool = False):
        self.usage = usage
        self.consume_allowed = consume_allowed
        self.fail_consume = fail_consume
        self.get_usage_calls = 0
        self.consume_calls = 0

    async def get_usage(self, key: str, redis_op: str | None = None) -> int:
        self.get_usage_calls += 1
        return self.usage

    async def check_available(self, key: str, max_limit: int, redis_op: str | None = None) -> bool:
        return self.usage < max_limit

    async def check_and_consume(self, key: str, daily_limit: int, ttl: int = 86400, idempotency_key=None, redis_op: str | None = None):
        self.consume_calls += 1
        if self.fail_consume:
            raise RuntimeError("redis unavailable")
        if not self.consume_allowed:
            return False, 0
        self.usage += 1
        return True, max(0, daily_limit - self.usage)


class FakeSearchService:
    should_fail = False

    def __init__(self, *_args, **_kwargs):
        pass

    async def run(self, **_kwargs):
        if self.should_fail:
            raise RuntimeError("boom before result")
        return SearchResponse(
            construction={"score": 5, "cached": False, "coord_key": "16.0199,108.2549"},
            demand=None,
            quota_remaining=3,
            checks_today=0,
            tier="free",
        )


class FakeQueryHistoryRepo:
    async def log_event(self, *_args, **_kwargs):
        return None


class FakeDemandService:
    async def record_query(self, *_args, **_kwargs):
        return True


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.api.routes.protect_mutation", lambda _request: _noop_async())
    monkeypatch.setattr("app.api.routes._emit_funnel_event", lambda *_args, **_kwargs: _noop_async())
    monkeypatch.setattr("app.api.routes.SearchService", FakeSearchService)
    monkeypatch.setattr("app.services.policy_engine.verify_turnstile", lambda *_args, **_kwargs: _true_async())
    FakeSearchService.should_fail = False
    with TestClient(app) as test_client:
        app.state.query_history_repo = FakeQueryHistoryRepo()
        app.state.demand_service = FakeDemandService()
        yield test_client


async def _noop_async():
    return None


async def _true_async():
    return True


async def _false_async():
    return False


def _payload(**overrides):
    payload = {
        "location_input": "16.0199444 108.2548611",
        "target": "construction",
        "turnstile_token": "valid-token",
    }
    payload.update(overrides)
    return payload


def test_valid_maps_url_fresh_anonymous_session_returns_result_and_consumes_after_result(client):
    quota = FakeQuotaRepo(usage=0)
    app.state.quota_repo = quota

    response = client.post("/api/search", json=_payload(location_input="https://www.google.com/maps/search/?api=1&query=16.0199444%2C108.2548611"))

    assert response.status_code == 200
    assert response.json()["construction"]["score"] == 5
    assert quota.get_usage_calls == 1
    assert quota.consume_calls == 1


def test_daily_quota_exhausted_returns_structured_429_without_increment(client):
    quota = FakeQuotaRepo(usage=3)
    app.state.quota_repo = quota

    response = client.post("/api/search", json=_payload())

    assert response.status_code == 429
    body = response.json()
    assert body["error"] == "FREE_DAILY_QUOTA_EXCEEDED"
    assert body["retry_after_seconds"] > 0
    assert body["remaining"] == 0
    assert quota.consume_calls == 0


def test_missing_turnstile_token_returns_403_without_quota_increment(client):
    quota = FakeQuotaRepo(usage=0)
    app.state.quota_repo = quota

    response = client.post("/api/search", json=_payload(turnstile_token=None))

    assert response.status_code == 403
    assert response.json() == {"error": "TURNSTILE_REQUIRED", "message": "Verification required."}
    assert quota.get_usage_calls == 0
    assert quota.consume_calls == 0


def test_invalid_turnstile_token_returns_403_without_quota_increment(client, monkeypatch):
    quota = FakeQuotaRepo(usage=0)
    app.state.quota_repo = quota
    monkeypatch.setattr("app.services.policy_engine.verify_turnstile", lambda *_args, **_kwargs: _false_async())

    response = client.post("/api/search", json=_payload(turnstile_token="invalid-token"))

    assert response.status_code == 403
    assert response.json() == {"error": "TURNSTILE_INVALID", "message": "Verification failed."}
    assert quota.consume_calls == 0


def test_backend_error_before_result_does_not_increment_quota(client):
    quota = FakeQuotaRepo(usage=0)
    app.state.quota_repo = quota
    FakeSearchService.should_fail = True

    response = client.post("/api/search", json=_payload())

    assert response.status_code == 500
    assert quota.consume_calls == 0


def test_server_backpressure_during_quota_increment_returns_structured_503(client):
    quota = FakeQuotaRepo(usage=0, fail_consume=True)
    app.state.quota_repo = quota

    response = client.post("/api/search", json=_payload())

    assert response.status_code == 503
    assert response.json() == {
        "error": "SEARCH_TEMPORARILY_THROTTLED",
        "message": "Service temporarily busy. Please try again in a moment.",
        "retry_after_seconds": 30,
    }
    assert quota.consume_calls == 1


def test_simulated_paid_missing_turnstile_token_returns_result_without_challenge(monkeypatch):
    async def fake_get_tier(*_args, **_kwargs):
        return EntitlementResult(tier=TierStatus.SIMULATED_PAID, daily_limit=5)

    monkeypatch.setattr("app.core.middleware.EntitlementService.get_tier", fake_get_tier)
    monkeypatch.setattr("app.api.routes.protect_mutation", lambda _request: _noop_async())
    monkeypatch.setattr("app.api.routes._emit_funnel_event", lambda *_args, **_kwargs: _noop_async())
    monkeypatch.setattr("app.api.routes.SearchService", FakeSearchService)
    monkeypatch.setattr("app.services.policy_engine.verify_turnstile", lambda *_args, **_kwargs: _false_async())
    FakeSearchService.should_fail = False
    quota = FakeQuotaRepo(usage=0)

    with TestClient(app) as test_client:
        app.state.quota_repo = quota
        app.state.query_history_repo = FakeQueryHistoryRepo()
        app.state.demand_service = FakeDemandService()
        response = test_client.post("/api/search", json=_payload(turnstile_token=None))

    assert response.status_code == 200
    assert response.json()["construction"]["score"] == 5
    assert quota.get_usage_calls == 1
    assert quota.consume_calls == 1
