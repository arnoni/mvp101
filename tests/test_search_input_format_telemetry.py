from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.search import SearchResponse


@dataclass
class GateResult:
    remaining_after: int = 2


class FakeSearchService:
    def __init__(self, *_args, **_kwargs):
        pass

    async def run(self, **_kwargs):
        return SearchResponse(
            construction={"score": 5, "cached": False},
            demand={"score": 6, "cached": False},
            quota_remaining=2,
            checks_today=1,
            tier="free",
        )


class FakeQueryHistoryRepo:
    async def log_event(self, *_args, **_kwargs):
        return None


class _FakeConn:
    async def execute(self, *_args, **_kwargs):
        return None


class _BeginCtx:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeDbEngine:
    def begin(self):
        return _BeginCtx()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("app.api.routes.protect_mutation", lambda _request: _noop_async())
    monkeypatch.setattr("app.api.routes._emit_funnel_event", lambda *_args, **_kwargs: _noop_async())
    monkeypatch.setattr("app.api.routes.SearchService", FakeSearchService)
    monkeypatch.setattr("app.api.routes.run_gate", lambda **_kwargs: _gate_async())

    with TestClient(app) as test_client:
        app.state.query_history_repo = FakeQueryHistoryRepo()
        app.state.db_engine = FakeDbEngine()
        yield test_client


async def _noop_async():
    return None


async def _gate_async():
    return GateResult()


def test_decimal_input_increments_before_success_response(client, monkeypatch):
    calls = []

    async def _increment(*_args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.api.routes.increment_input_format_stats", _increment)

    response = client.post(
        "/api/search",
        json={"location_input": "16.0199444 108.2548611", "target": "construction", "turnstile_token": "test-token"},
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["input_format"] == "decimal_coordinates"
    assert calls[0]["input_parse_status"] == "parsed"


def test_google_short_link_increments_with_recognized_not_resolved(client, monkeypatch):
    calls = []

    async def _increment(*_args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.api.routes.increment_input_format_stats", _increment)

    response = client.post(
        "/api/search",
        json={"location_input": "https://maps.app.goo.gl/QEWSpGGS7iLZomR98", "target": "construction", "turnstile_token": "test-token"},
    )
    assert response.status_code == 422
    assert len(calls) == 1
    assert calls[0]["input_format"] == "google_maps_short_url"
    assert calls[0]["input_parse_status"] == "recognized_not_resolved"
    assert calls[0]["input_host"] == "maps.app.goo.gl"


def test_over_quota_path_still_increments(client, monkeypatch):
    calls = []

    async def _increment(*_args, **kwargs):
        calls.append(kwargs)

    async def _deny_gate(**_kwargs):
        raise HTTPException(status_code=429, detail="over quota")

    monkeypatch.setattr("app.api.routes.increment_input_format_stats", _increment)
    monkeypatch.setattr("app.api.routes.run_gate", _deny_gate)

    response = client.post(
        "/api/search",
        json={"location_input": "16.0199444 108.2548611", "target": "construction", "turnstile_token": "test-token"},
    )
    assert response.status_code == 429
    assert len(calls) == 1


def test_turnstile_failed_path_still_increments(client, monkeypatch):
    calls = []

    async def _increment(*_args, **kwargs):
        calls.append(kwargs)

    async def _deny_gate(**_kwargs):
        raise HTTPException(status_code=403, detail="turnstile failed")

    monkeypatch.setattr("app.api.routes.increment_input_format_stats", _increment)
    monkeypatch.setattr("app.api.routes.run_gate", _deny_gate)

    response = client.post(
        "/api/search",
        json={"location_input": "16.0199444 108.2548611", "target": "construction", "turnstile_token": "test-token"},
    )
    assert response.status_code == 403
    assert len(calls) == 1


def test_invalid_input_still_increments(client, monkeypatch):
    calls = []

    async def _increment(*_args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.api.routes.increment_input_format_stats", _increment)

    response = client.post(
        "/api/search",
        json={"location_input": "23 Phước Trường 10, An Hải, Sơn Trà, Đà Nẵng", "target": "construction", "turnstile_token": "test-token"},
    )
    assert response.status_code == 422
    assert len(calls) == 1
    assert calls[0]["input_format"] == "plain_address"


def test_telemetry_failure_is_fail_open(client, monkeypatch, caplog):
    async def _explode(*_args, **_kwargs):
        raise RuntimeError("telemetry db down")

    monkeypatch.setattr("app.api.routes.increment_input_format_stats", _explode)

    response = client.post(
        "/api/search",
        json={"location_input": "16.0199444 108.2548611", "target": "construction", "turnstile_token": "test-token"},
    )
    assert response.status_code == 200
    assert "input_format_stats_increment_failed" in caplog.text


def test_no_raw_input_sent_to_aggregate_increment(client, monkeypatch):
    calls = []

    async def _increment(*_args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.api.routes.increment_input_format_stats", _increment)

    raw_value = "please check this place 16.0199444 108.2548611 thanks"
    response = client.post(
        "/api/search",
        json={"location_input": raw_value, "target": "construction", "turnstile_token": "test-token"},
    )
    assert response.status_code == 200
    assert len(calls) == 1
    assert "raw_input" not in calls[0]
    assert raw_value not in str(calls[0])


def test_missing_location_returns_400_with_contract_payload(client, monkeypatch):
    calls = []

    async def _increment(*_args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("app.api.routes.increment_input_format_stats", _increment)

    response = client.post("/api/search", json={"target": "construction", "turnstile_token": "test-token"})
    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "status": "missing_location",
            "message": "Enter coordinates or a Google Maps URL to generate a report.",
        }
    }
    assert len(calls) == 0
