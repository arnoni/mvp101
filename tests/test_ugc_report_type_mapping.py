from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils.security import verify_turnstile_dependency


class _InsertResult:
    def __init__(self, public_id: str) -> None:
        self.public_id = public_id

    def first(self):
        return SimpleNamespace(public_id=self.public_id)


class _RecordingConnection:
    def __init__(self, engine: "_RecordingEngine") -> None:
        self.engine = engine

    async def execute(self, _sql, params):
        self.engine.inserts.append(params)
        return _InsertResult(params["public_id"])


class _RecordingBegin:
    def __init__(self, engine: "_RecordingEngine") -> None:
        self.engine = engine

    async def __aenter__(self):
        return _RecordingConnection(self.engine)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingEngine:
    def __init__(self) -> None:
        self.inserts: list[dict] = []

    def begin(self):
        return _RecordingBegin(self)


@pytest.fixture
def client():
    engine = _RecordingEngine()
    app.state.redis = None
    app.state.db_engine = engine
    app.state.quota_repo = object()
    app.dependency_overrides[verify_turnstile_dependency] = lambda: True
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(verify_turnstile_dependency, None)


def _payload(report_type: str) -> dict:
    return {
        "title": "Construction noise",
        "description": "Heavy equipment operating next door",
        "lat": 16.0544,
        "lon": 108.2022,
        "report_type": report_type,
    }


@pytest.mark.parametrize(
    ("report_type", "expected_category", "expected_severity"),
    [
        ("active", "active_construction", 5),
        ("maybe", "maybe_construction", 3),
        ("ended", "construction_ended", 1),
    ],
)
def test_ugc_report_type_maps_to_severity(client, report_type, expected_category, expected_severity):
    response = client.post("/api/ugc/report-submit", json=_payload(report_type))

    assert response.status_code == 200
    assert len(client.app.state.db_engine.inserts) == 1
    row = client.app.state.db_engine.inserts[0]
    assert row["category"] == expected_category
    assert row["severity"] == expected_severity


def test_invalid_ugc_report_type_is_rejected(client):
    response = client.post("/api/ugc/report-submit", json=_payload("demolition"))

    assert response.status_code == 422
    assert client.app.state.db_engine.inserts == []


def test_frontend_cannot_override_severity(client):
    payload = _payload("maybe") | {"severity": 5}

    response = client.post("/api/ugc/report-submit", json=payload)

    assert response.status_code == 422
    assert client.app.state.db_engine.inserts == []
