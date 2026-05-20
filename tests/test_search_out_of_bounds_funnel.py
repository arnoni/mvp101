from fastapi.testclient import TestClient

from app.main import app
from app.schemas.search import SearchResponse


class FakeQuotaRepo:
    async def get_usage(self, key: str, redis_op: str | None = None) -> int:
        return 0

    async def check_available(self, key: str, max_limit: int, redis_op: str | None = None) -> bool:
        return True

    async def check_and_consume(self, key: str, daily_limit: int, ttl: int = 86400, idempotency_key=None, redis_op: str | None = None):
        return True, max(0, daily_limit - 1)


class FakeSearchService:
    def __init__(self, *_args, **_kwargs):
        pass

    async def run(self, **_kwargs):
        return SearchResponse(
            construction={"score": 5, "cached": False, "coord_key": "16.0199,108.2549"},
            demand=None,
            quota_remaining=3,
            checks_today=0,
            tier="free",
        )


async def _noop_async():
    return None


async def _true_async():
    return True


def _payload(**overrides):
    payload = {
        "location_input": "16.0199444 108.2548611",
        "target": "construction",
        "turnstile_token": "valid-token",
    }
    payload.update(overrides)
    return payload


def test_search_out_of_bounds_emits_funnel_event(monkeypatch):
    calls = []

    async def _emit(*_args, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr("app.api.routes.protect_mutation", lambda _request: _noop_async())
    monkeypatch.setattr("app.api.routes._emit_funnel_event", _emit)
    monkeypatch.setattr("app.api.routes.SearchService", FakeSearchService)
    monkeypatch.setattr("app.services.policy_engine.verify_turnstile", lambda *_args, **_kwargs: _true_async())
    app.state.quota_repo = FakeQuotaRepo()

    with TestClient(app) as client:
        response = client.post("/api/search", json=_payload(location_input="20.0, 108.2"))

    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "OUT_OF_BOUNDS"
    assert len(calls) == 1
    assert calls[0]["event_name"] == "search_out_of_bounds"
    assert calls[0]["metadata_json"]["parsed_latitude"] == 20.0
    assert calls[0]["metadata_json"]["rejection_reason"] == "outside_app_bounding_box"


def test_search_inside_bbox_does_not_emit_out_of_bounds_event(monkeypatch):
    calls = []

    async def _emit(*_args, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr("app.api.routes.protect_mutation", lambda _request: _noop_async())
    monkeypatch.setattr("app.api.routes._emit_funnel_event", _emit)
    monkeypatch.setattr("app.api.routes.SearchService", FakeSearchService)
    monkeypatch.setattr("app.services.policy_engine.verify_turnstile", lambda *_args, **_kwargs: _true_async())
    app.state.quota_repo = FakeQuotaRepo()

    with TestClient(app) as client:
        response = client.post("/api/search", json=_payload(location_input="16.0199444 108.2548611"))

    assert response.status_code == 200
    assert all(c.get("event_name") != "search_out_of_bounds" for c in calls)
