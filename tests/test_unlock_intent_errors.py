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
        self.executed_statements = []

    async def execute(self, _statement):
        self.executed_statements.append(str(_statement))
        self._step += 1
        if self._step == 1:  # feature flag is_enabled
            return _FakeResult(True)
        if self._step == 2:  # user upsert returning user id
            return _FakeResult("user-123")
        if self._step == 3:  # active simulated plan code
            return _FakeResult("1_day_test_a")
        return _FakeResult(None)


class _FakeBeginCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self):
        self.conn = _FakeConnection()

    def begin(self):
        return _FakeBeginCtx(self.conn)


def test_unlock_intent_missing_turnstile_returns_structured_error_id(monkeypatch):
    async def _noop_protect_mutation(_request):
        return None

    monkeypatch.setattr("app.api.billing.protect_mutation", _noop_protect_mutation)

    with TestClient(app) as client:
        response = client.post(
            "/api/billing/unlock-intent",
            json={"email": "test@example.com", "plan": "sim_1_day"},
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
                "plan": "sim_1_day",
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
    fake_engine = _FakeEngine()
    monkeypatch.setattr(app.state, "db_engine", fake_engine, raising=False)

    with TestClient(app) as client:
        response = client.post(
            "/api/billing/unlock-intent",
            json={
                "email": "test@example.com",
                "plan": "sim_1_day",
                "turnstile_token": "dummy-token",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] in {"magic_link_sent", "intent_created"}
    assert body["intent_id"]
    joined_statements = "\n".join(fake_engine.conn.executed_statements)
    assert "simulated_billing_plans" in joined_statements
    assert " billing_plans " not in joined_statements


def test_unlock_intent_rejects_unsupported_sim_3_day(monkeypatch):
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
                "plan": "sim_3_day",
                "turnstile_token": "dummy-token",
            },
        )

    assert response.status_code == 400
    body = response.json()
    detail = body["detail"]
    assert detail["detail"] == "Unsupported simulated access duration"
