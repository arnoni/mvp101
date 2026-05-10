from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.keys import KeyBuilder
from app.main import app
from app.schemas.search import GaugeResult, SearchResponse, SearchTarget
from app.services.entitlement_service import EntitlementResult, TierStatus
from app.services.quota_service import compute_construction_fingerprint


class _Result:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class SearchQuotaDB:
    def __init__(self, *, user_id: uuid.UUID, remaining: int | None = 5):
        self.user_id = user_id
        self.remaining = remaining
        self.fingerprints: set[str] = set()
        self.insert_count = 0
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, sql, params=None):
        statement = str(sql).lower()
        params = params or {}
        fingerprint = params.get("fingerprint")
        if "from construction_queries" in statement:
            return _Result(SimpleNamespace(found=1) if fingerprint in self.fingerprints else None)
        if "select remaining_quota" in statement and "for update" in statement:
            return _Result(SimpleNamespace(remaining_quota=self.remaining))
        if "insert into construction_queries" in statement:
            if fingerprint not in self.fingerprints:
                self.insert_count += 1
            self.fingerprints.add(fingerprint)
            return _Result()
        if "set remaining_quota = remaining_quota - 1" in statement:
            if self.remaining is None or self.remaining <= 0:
                return _Result(None)
            self.remaining -= 1
            return _Result(SimpleNamespace(remaining_quota=self.remaining))
        if "when remaining_quota is null then :daily_limit" in statement:
            if self.remaining is None:
                self.remaining = int(params["daily_limit"])
            return _Result(SimpleNamespace(remaining_quota=self.remaining))
        if "set remaining_quota = :daily_limit" in statement:
            self.remaining = int(params["daily_limit"])
            return _Result()
        raise AssertionError(f"unexpected SQL: {statement}")

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeAsyncSession:
    db: SearchQuotaDB | None = None

    def __init__(self, *_args, **_kwargs):
        if self.db is None:
            raise AssertionError("FakeAsyncSession.db was not configured")
        self._db = self.db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc_info):
        return False


class FakeQuotaRepo:
    def __init__(self, usage: int = 0, consume_allowed: bool = True):
        self.usage = usage
        self.consume_allowed = consume_allowed
        self.get_usage_calls = 0
        self.consume_calls = 0

    async def get_usage(self, key: str, redis_op: str | None = None) -> int:
        self.get_usage_calls += 1
        return self.usage

    async def check_available(self, key: str, max_limit: int, redis_op: str | None = None) -> bool:
        return self.usage < max_limit

    async def check_and_consume(
        self,
        key: str,
        daily_limit: int,
        ttl: int = 86400,
        idempotency_key=None,
        redis_op: str | None = None,
    ):
        self.consume_calls += 1
        if not self.consume_allowed:
            return False, 0
        self.usage += 1
        return True, max(0, daily_limit - self.usage)


class FakeSearchService:
    def __init__(self, *_args, **_kwargs):
        pass

    async def run(self, *, request, tier: str, quota_remaining: int, checks_today: int):
        construction = None
        demand = None
        if request.target in (SearchTarget.CONSTRUCTION, SearchTarget.BOTH):
            construction = GaugeResult(score=42, cached=False, coord_key=f"{request.lat:.4f},{request.lon:.4f}")
        if request.target in (SearchTarget.DEMAND, SearchTarget.BOTH):
            demand = GaugeResult(score=7, cached=False, coord_key=f"{request.lat:.4f},{request.lon:.4f}")
        return SearchResponse(
            construction=construction,
            demand=demand,
            message_code="SEARCH_COMPLETE",
            message="ok",
            quota_remaining=quota_remaining,
            checks_today=checks_today,
            tier=tier,
        )


class FakeDemandService:
    async def record_query(self, *_args, **_kwargs):
        return True

    async def get_demand_rolling(self, *_args, **_kwargs):
        return 1


class FakePrecomputeRepo:
    async def get_candidates(self, *_args, **_kwargs):
        return []


class FakeQueryHistoryRepo:
    async def log_event(self, *_args, **_kwargs):
        return None


class FakeIdentityRedis:
    def __init__(self, *, user_id: uuid.UUID):
        self.user_id = user_id

    async def get(self, key: str):
        if key == KeyBuilder.session("test-session"):
            return json.dumps({"user_id": str(self.user_id), "email": "user@example.com"})
        return None


async def _noop_async(*_args, **_kwargs):
    return None


async def _true_async(*_args, **_kwargs):
    return True


@pytest.fixture
def quota_client(monkeypatch):
    user_id = uuid.uuid4()
    db = SearchQuotaDB(user_id=user_id, remaining=5)
    quota_repo = FakeQuotaRepo(usage=0)

    FakeAsyncSession.db = db
    monkeypatch.setattr("app.api.routes.AsyncSession", FakeAsyncSession)
    monkeypatch.setattr("app.api.routes.SearchService", FakeSearchService)
    monkeypatch.setattr("app.api.routes._emit_funnel_event", _noop_async)
    monkeypatch.setattr("app.api.routes.capture", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.api.routes.protect_mutation", _noop_async)
    monkeypatch.setattr("app.services.policy_engine.verify_turnstile", _true_async)
    monkeypatch.setattr("app.services.redis_client.redis_client", FakeIdentityRedis(user_id=user_id))

    async def fake_get_tier(user_id_arg, *_args, **_kwargs):
        if user_id_arg:
            return EntitlementResult(tier=TierStatus.SIMULATED_PAID, daily_limit=5)
        return EntitlementResult(tier=TierStatus.FREE, daily_limit=3)

    monkeypatch.setattr("app.core.middleware.EntitlementService.get_tier", fake_get_tier)

    with TestClient(app) as client:
        app.state.quota_repo = quota_repo
        app.state.precompute_repo = FakePrecomputeRepo()
        app.state.demand_service = FakeDemandService()
        app.state.query_history_repo = FakeQueryHistoryRepo()
        app.state.db_engine = object()
        app.state.redis = None
        yield SimpleNamespace(client=client, db=db, quota_repo=quota_repo, user_id=user_id)

    FakeAsyncSession.db = None


def _payload(target: str, *, lat: float = 16.0199444, lon: float = 108.2548611):
    return {"lat": lat, "lon": lon, "target": target, "turnstile_token": "valid-token"}


def _auth_cookies():
    return {settings.SESSION_COOKIE_NAME: "test-session"}


def _fingerprint(lat: float = 16.0199444, lon: float = 108.2548611):
    return compute_construction_fingerprint(lat, lon, radius_m=50)


def test_construction_authenticated_decrements_quota(quota_client):
    response = quota_client.client.post("/api/search", json=_payload("construction"), cookies=_auth_cookies())

    assert response.status_code == 200
    body = response.json()
    assert body["construction"] is not None
    assert body["quota"] == {
        "consumed": True,
        "remaining": 4,
        "effective_tier": "simulated_paid",
        "reason": "new_construction_query",
    }
    assert quota_client.db.remaining == 4
    assert _fingerprint() in quota_client.db.fingerprints
    assert quota_client.db.insert_count == 1
    assert quota_client.quota_repo.consume_calls == 0


def test_construction_same_location_twice_no_double_charge(quota_client):
    first = quota_client.client.post("/api/search", json=_payload("construction"), cookies=_auth_cookies())
    second = quota_client.client.post("/api/search", json=_payload("construction"), cookies=_auth_cookies())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["quota"]["consumed"] is True
    assert second.json()["quota"]["consumed"] is False
    assert second.json()["quota"]["reason"] == "duplicate_construction_query_no_charge"
    assert quota_client.db.remaining == 4
    assert quota_client.db.insert_count == 1


def test_demand_after_construction_no_charge(quota_client):
    construction = quota_client.client.post("/api/search", json=_payload("construction"), cookies=_auth_cookies())
    demand = quota_client.client.post("/api/search", json=_payload("demand"), cookies=_auth_cookies())

    assert construction.status_code == 200
    assert demand.status_code == 200
    body = demand.json()
    assert body["demand"] is not None
    assert body["quota"] == {
        "consumed": False,
        "remaining": 4,
        "effective_tier": "simulated_paid",
        "reason": "demand_report_no_charge",
    }
    assert quota_client.db.remaining == 4
    assert quota_client.db.insert_count == 1


def test_demand_without_prior_construction_returns_422(quota_client):
    response = quota_client.client.post("/api/search", json=_payload("demand"), cookies=_auth_cookies())

    assert response.status_code == 422
    assert response.json() == {
        "error": "no_prior_construction_query",
        "message": "Request a construction report for this location first.",
    }
    assert quota_client.db.remaining == 5
    assert quota_client.db.insert_count == 0


def test_demand_repeated_returns_no_charge(quota_client):
    quota_client.client.post("/api/search", json=_payload("construction"), cookies=_auth_cookies())
    first = quota_client.client.post("/api/search", json=_payload("demand"), cookies=_auth_cookies())
    second = quota_client.client.post("/api/search", json=_payload("demand"), cookies=_auth_cookies())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["quota"]["consumed"] is False
    assert second.json()["quota"]["consumed"] is False
    assert quota_client.db.remaining == 4
    assert quota_client.db.insert_count == 1


def test_anonymous_construction_uses_redis_not_postgres(quota_client):
    response = quota_client.client.post("/api/search", json=_payload("construction"))

    assert response.status_code == 200
    assert response.json()["construction"] is not None
    assert response.json()["quota"] is None
    assert quota_client.quota_repo.consume_calls == 1
    assert quota_client.db.insert_count == 0
    assert quota_client.db.remaining == 5


def test_anonymous_demand_no_quota_consumed(quota_client):
    response = quota_client.client.post("/api/search", json=_payload("demand"))

    assert response.status_code == 200
    body = response.json()
    assert body["demand"] is not None
    assert body["quota"] is None
    assert quota_client.quota_repo.consume_calls == 0
    assert quota_client.db.insert_count == 0


def test_quota_exhausted_returns_402(quota_client):
    quota_client.db.remaining = 0

    response = quota_client.client.post("/api/search", json=_payload("construction"), cookies=_auth_cookies())

    assert response.status_code == 402
    assert response.json() == {
        "error": "quota_exceeded",
        "quota": {
            "consumed": False,
            "remaining": 0,
            "effective_tier": "simulated_paid",
            "reason": "insufficient_quota",
        },
    }
    assert quota_client.db.remaining == 0
    assert quota_client.db.insert_count == 0


def test_both_target_decrements_quota_by_one_only(quota_client):
    response = quota_client.client.post("/api/search", json=_payload("both"), cookies=_auth_cookies())

    assert response.status_code == 200
    body = response.json()
    assert body["construction"] is not None
    assert body["demand"] is not None
    assert body["quota"]["consumed"] is True
    assert body["quota"]["remaining"] == 4
    assert quota_client.db.remaining == 4
    assert quota_client.db.insert_count == 1


def test_response_contains_quota_block_for_authenticated(quota_client):
    response = quota_client.client.post("/api/search", json=_payload("construction"), cookies=_auth_cookies())

    assert response.status_code == 200
    quota = response.json()["quota"]
    assert quota["consumed"] is True
    assert quota["remaining"] == 4
    assert quota["effective_tier"] == "simulated_paid"
    assert quota["reason"] == "new_construction_query"


def test_response_quota_is_none_for_anonymous(quota_client):
    response = quota_client.client.post("/api/search", json=_payload("construction"))

    assert response.status_code == 200
    assert response.json()["quota"] is None


def test_invalid_target_validation(quota_client):
    response = quota_client.client.post("/api/search", json=_payload("invalid"), cookies=_auth_cookies())

    assert response.status_code == 422
    body = response.json()
    assert "detail" in body
    assert quota_client.db.remaining == 5
    assert quota_client.db.insert_count == 0
