from fastapi.testclient import TestClient

from app.main import app


class _FakeResult:
    def __init__(self, scalar_value=None):
        self._scalar_value = scalar_value

    def scalar_one_or_none(self):
        return self._scalar_value

    def scalar_one(self):
        if self._scalar_value is None:
            raise ValueError("Expected scalar value")
        return self._scalar_value


class _FakeConnection:
    def __init__(self):
        self._step = 0

    async def execute(self, _statement):
        self._step += 1
        if self._step == 1:  # feature flag is_enabled
            return _FakeResult(True)
        if self._step == 2:  # user upsert returning user id
            return _FakeResult("user-123")
        if self._step == 3:  # active simulated plan code
            return _FakeResult("1_day")
        return _FakeResult(None)


class _FakeBeginCtx:
    async def __aenter__(self):
        return _FakeConnection()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def begin(self):
        return _FakeBeginCtx()


def test_unlock_intent_missing_turnstile_returns_structured_error_id(monkeypatch):
    async def _noop_protect_mutation(_request):
        return None

    monkeypatch.setattr("app.api.billing.protect_mutation", _noop_protect_mutation)

    with TestClient(app) as client:
        response = client.post(
            "/api/billing/unlock-intent",
            json={"email": "test@example.com", "plan": "1_day"},
        )

    assert response.status_code == 400
    body = response.json()
    detail = body["detail"]
    assert detail["error"] == "HTTP_ERROR"
    assert detail["detail"] == "Turnstile token required"
    assert detail["status_code"] == 400
    assert isinstance(detail["error_id"], str)
    assert detail["error_id"]


def test_unlock_intent_db_missing_returns_structured_error_id(monkeypatch):
    async def _noop_protect_mutation(_request):
        return None

    async def _valid_turnstile(*_args, **_kwargs):
        return True

    monkeypatch.setattr("app.api.billing.protect_mutation", _noop_protect_mutation)
    monkeypatch.setattr("app.api.billing.verify_turnstile", _valid_turnstile)

    with TestClient(app) as client:
        response = client.post(
            "/api/billing/unlock-intent",
            json={
                "email": "test@example.com",
                "plan": "1_day",
                "turnstile_token": "dummy-token",
            },
        )

    assert response.status_code == 503
    body = response.json()
    detail = body["detail"]
    assert detail["error"] == "HTTP_ERROR"
    assert detail["detail"] == "Database is not configured"
    assert detail["status_code"] == 503
    assert isinstance(detail["error_id"], str)
    assert detail["error_id"]


def test_unlock_intent_happy_path_returns_checkout_url(monkeypatch):
    async def _noop_protect_mutation(_request):
        return None

    async def _valid_turnstile(*_args, **_kwargs):
        return True

    monkeypatch.setattr("app.api.billing.protect_mutation", _noop_protect_mutation)
    monkeypatch.setattr("app.api.billing.verify_turnstile", _valid_turnstile)
    monkeypatch.setattr(app.state, "db_engine", _FakeEngine(), raising=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/billing/unlock-intent",
            json={
                "email": "test@example.com",
                "plan": "1_day",
                "turnstile_token": "dummy-token",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert "checkout_url" in body
    assert body["checkout_url"].endswith("/?simulated_unlock=initiated")
    assert body["intent_id"]
