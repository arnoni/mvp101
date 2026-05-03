from fastapi.testclient import TestClient

from app.main import app


def _payload(token: str | None):
    return {
        "lat": 16.0745,
        "lon": 108.244,
        "report_kind": "active_construction",
        "is_nearby_now": True,
        "note": "construction noise all day",
        "cf_turnstile_token": token,
        "location_source": "manual_input",
    }


def test_user_report_missing_turnstile_returns_403_not_500():
    with TestClient(app) as client:
        res = client.post("/api/user-reports", json=_payload(None))

    assert res.status_code == 403
    body = res.json()
    assert body["code"] == "TURNSTILE_REQUIRED"
    assert body["request_id"]


def test_user_report_invalid_turnstile_returns_403_not_500(monkeypatch):
    async def _invalid_turnstile(*_args, **_kwargs):
        return False

    monkeypatch.setattr("app.api.routes.verify_turnstile", _invalid_turnstile)

    with TestClient(app) as client:
        res = client.post("/api/user-reports", json=_payload("bad-token"))

    assert res.status_code == 403
    body = res.json()
    assert body["code"] == "TURNSTILE_FAILED"
    assert body["request_id"]


def test_user_report_valid_turnstile_calls_submit_and_returns_success(monkeypatch):
    called = {}

    async def _valid_turnstile(*_args, **_kwargs):
        return True

    async def _fake_submit(*_args, **_kwargs):
        called["ok"] = True
        return {"ok": True, "report_id": "rpt_123", "duplicate": False}

    monkeypatch.setattr("app.api.routes.verify_turnstile", _valid_turnstile)
    monkeypatch.setattr("app.api.routes.ugc_report_submit", _fake_submit)

    with TestClient(app) as client:
        res = client.post("/api/user-reports", json=_payload("valid-token"))

    assert res.status_code == 200
    assert called.get("ok") is True
    body = res.json()
    assert body["ok"] is True
    assert body["report_id"] == "rpt_123"
