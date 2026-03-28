from fastapi.testclient import TestClient

from app.main import app


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
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["error_code"] in {"UNSUPPORTED_LOCATION_INPUT", "INVALID_LOCATION_INPUT"}
