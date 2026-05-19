from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.services.location_parser import LocationResolutionBlockedError
TASHI_BROWSER_URL = "https://www.google.com/maps/place/Tashi+Ocean+Garden+Hotel+%26+Apartment+Da+Nang/@16.077462,108.2419443,19z/data=!4m20!1m8!3m7!1s0x314217893f8ae817:0xb2eb103f179a78ed!2zMjEgUGjGsOG7m2MgVHLGsOG7nW5nIDExLCBBbiBI4bqjaSwgxJDDoCBO4bq1bmcgNTUwMDAwLCBWaWV0bmFt!3b1!8m2!3d16.0777299!4d108.242299!16s%2Fg%2F11jyly4fyb!3m10!1s0x31421714368a092b:0x318717f306c12aec!5m4!1s2026-05-23!2i3!4m1!1i2!8m2!3d16.0776236!4d108.2426482!16s%2Fg%2F11kbp5srhq!18m1!1e1?entry=ttu&g_ep=EgoyMDI2MDUxMy4wIKXMDSoASAFQAw%3D%3D"


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
    async def _raise_blocked(_raw: str, **_kwargs):
        raise LocationResolutionBlockedError(
            "This Google Maps short link could not be resolved automatically. Please paste the full address, coordinates, or open the link and share the location text."
        )

    monkeypatch.setattr("app.api.routes.parse_location_input_async", _raise_blocked)

    with TestClient(app) as client:
        response = client.post("/api/parse-location", json={"location_input": "https://maps.app.goo.gl/blocked"})
        assert response.status_code == 422
        body = response.json()
        assert body["detail"]["error"]["code"] == "LOCATION_RESOLUTION_FAILED"


def test_parse_location_unexpected_parser_error_returns_422(monkeypatch):
    async def _raise_unexpected(_raw: str, **_kwargs):
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr("app.api.routes.parse_location_input_async", _raise_unexpected)

    with TestClient(app) as client:
        response = client.post("/api/parse-location", json={"location_input": "broken-input"})
        assert response.status_code == 422
        body = response.json()
        assert body["detail"]["error"] == "LOCATION_PARSE_FAILED"
        assert body["detail"]["error_code"] == "INVALID_LOCATION_INPUT"


def test_parse_location_browser_google_url_success():
    with TestClient(app) as client:
        response = client.post("/api/parse-location", json={"location_input": TASHI_BROWSER_URL})
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["normalized"]["latitude"] == pytest.approx(16.0777299)
        assert body["normalized"]["longitude"] == pytest.approx(108.242299)


def test_parse_location_short_url_success_via_async_handoff(monkeypatch):
    async def _stub(_raw: str, **_kwargs):
        from app.services.location_parser import parse_google_maps_url
        return parse_google_maps_url(TASHI_BROWSER_URL)

    monkeypatch.setattr("app.api.routes.parse_location_input_async", _stub)
    with TestClient(app) as client:
        response = client.post("/api/parse-location", json={"location_input": "https://maps.app.goo.gl/gTP5u9ELrzyaCU37A"})
        assert response.status_code == 200
