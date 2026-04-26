from fastapi.testclient import TestClient

from app.main import app
from app.api.auth import AuthResponse


class _Result:
    def __init__(self, *, scalar=None, mapping=None, rowcount=None):
        self._scalar = scalar
        self._mapping = mapping
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        if self._scalar is None:
            raise ValueError("Expected scalar value")
        return self._scalar

    def mappings(self):
        return self

    def first(self):
        return self._mapping


class _UnlockConn:
    def __init__(self, recorder):
        self._step = 0
        self.recorder = recorder

    async def execute(self, statement):
        self._step += 1
        table_name = getattr(getattr(statement, "table", None), "name", None)
        if table_name == "simulated_payment_intents":
            self.recorder["simulated_payment_intents_inserts"] += 1
        if self._step == 1:  # feature flag
            return _Result(scalar=True)
        if self._step == 2:  # upserted user id
            return _Result(scalar="user-123")
        if self._step == 3:  # active plan code
            return _Result(scalar="1_day")
        return _Result(scalar=None)


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _UnlockEngine:
    def __init__(self):
        self.recorder = {"simulated_payment_intents_inserts": 0}

    def begin(self):
        return _Ctx(_UnlockConn(self.recorder))


class _FakeAuthService:
    def __init__(self, db):
        self.db = db
        self.redis = object()

    async def create_magic_link(self, **_kwargs):
        return "raw-token"


async def _noop_protect_mutation(_request):
    return None


async def _always_valid_turnstile(*_args, **_kwargs):
    return True


def test_unlock_intent_inserts_new_simulated_payment_intent_per_valid_submission(monkeypatch):
    engine = _UnlockEngine()
    monkeypatch.setattr("app.api.billing.protect_mutation", _noop_protect_mutation)
    monkeypatch.setattr("app.api.billing.verify_turnstile", _always_valid_turnstile)
    monkeypatch.setattr(app.state, "db_engine", engine, raising=False)

    with TestClient(app) as client:
        for idx in range(2):
            response = client.post(
                "/api/billing/unlock-intent",
                json={
                    "email": f"sim{idx}@example.com",
                    "plan": "1_day",
                    "turnstile_token": "dummy-token",
                },
            )
            assert response.status_code == 200

    assert engine.recorder["simulated_payment_intents_inserts"] == 2


def test_login_deprecated_wrapper_uses_unified_magic_link_logic(monkeypatch):
    captured = {}

    async def _fake_resend_impl(payload, request, *, enforce_turnstile):
        captured["email"] = payload.email
        captured["enforce_turnstile"] = enforce_turnstile
        return AuthResponse(message="If this email is eligible, we sent a new access link.")

    async def _fake_get_auth_service(_request):
        return _FakeAuthService(_UnlockEngine())

    app.dependency_overrides = {__import__("app.api.auth", fromlist=["get_auth_service"]).get_auth_service: _fake_get_auth_service}
    monkeypatch.setattr("app.api.auth._resend_magic_link_impl", _fake_resend_impl)

    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"email": "no-intent@example.com"})

    app.dependency_overrides = {}

    assert response.status_code == 200
    assert response.json()["message"] == "If this email is eligible, we sent a new access link."
    assert captured["email"] == "no-intent@example.com"
    assert captured["enforce_turnstile"] is False


def test_login_deprecated_wrapper_preserves_generic_message(monkeypatch):
    async def _fake_resend_impl(payload, request, *, enforce_turnstile):
        return AuthResponse(message="If this email is eligible, we sent a new access link.")

    async def _fake_get_auth_service(_request):
        return _FakeAuthService(_UnlockEngine())

    app.dependency_overrides = {__import__("app.api.auth", fromlist=["get_auth_service"]).get_auth_service: _fake_get_auth_service}
    monkeypatch.setattr("app.api.auth._resend_magic_link_impl", _fake_resend_impl)

    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"email": "with-intent@example.com"})

    app.dependency_overrides = {}

    assert response.status_code == 200
    assert response.json()["message"] == "If this email is eligible, we sent a new access link."
