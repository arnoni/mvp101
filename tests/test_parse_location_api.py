from fastapi.testclient import TestClient

from app.main import app
from app.services.location_parser import LocationResolutionBlockedError


def test_parse_location_decimal_success():
    with TestClient(app) as client:
        response = client.post("/api/parse-location", json={"location_input": "16.0544,108.2022"})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["normalized"]["input_kind"] == "decimal_pair"


def test_parse_location_invalid_url_error():
    with TestClient(app) as client:
        response = client.post("/api/parse-location", json={"location_input": "https://example.com"})
        assert response.status_code == 422
        body = response.json()
        detail = body["detail"]
        assert detail["error"] == "LOCATION_PARSE_FAILED"
        assert detail["error_code"] in {"UNSUPPORTED_LOCATION_INPUT", "INVALID_LOCATION_INPUT"}


def test_parse_location_short_link_blocked_error(monkeypatch):
    def _raise_blocked(_raw: str):
        raise LocationResolutionBlockedError(
            "This Google Maps short link could not be resolved automatically. Please paste the full address, coordinates, or open the link and share the location text."
        )

    monkeypatch.setattr("app.api.routes.parse_location_input", _raise_blocked)

    with TestClient(app) as client:
        response = client.post("/api/parse-location", json={"location_input": "https://maps.app.goo.gl/blocked"})
        assert response.status_code == 422
        body = response.json()
        assert body["detail"]["error"]["code"] == "LOCATION_RESOLUTION_FAILED"


def test_parse_location_unexpected_parser_error_returns_422(monkeypatch):
    def _raise_unexpected(_raw: str):
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr("app.api.routes.parse_location_input", _raise_unexpected)

    with TestClient(app) as client:
        response = client.post("/api/parse-location", json={"location_input": "broken-input"})
        assert response.status_code == 422
        body = response.json()
        assert body["detail"]["error"] == "LOCATION_PARSE_FAILED"
        assert body["detail"]["error_code"] == "INVALID_LOCATION_INPUT"
