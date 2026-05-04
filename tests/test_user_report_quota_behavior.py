from fastapi.testclient import TestClient

from app.main import app


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.expire_map = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        if ex:
            self.expire_map[key] = ex
        return True

    async def incr(self, key):
        v = int(self.store.get(key, 0)) + 1
        self.store[key] = v
        return v

    async def expire(self, key, seconds):
        self.expire_map[key] = seconds
        return True

    async def decr(self, key):
        v = int(self.store.get(key, 0)) - 1
        self.store[key] = v
        return v


def _payload(token="ok"):
    return {
        "lat": 16.0745,
        "lon": 108.244,
        "report_kind": "active_construction",
        "is_nearby_now": True,
        "note": "construction noise all day",
        "cf_turnstile_token": token,
        "location_source": "manual_input",
    }


def test_success_increments_success_quota_once(monkeypatch):
    fake = FakeRedis()
    app.state.redis = fake
    async def _valid_turnstile(*_args, **_kwargs):
        return True

    monkeypatch.setattr("app.api.routes.verify_turnstile", _valid_turnstile)

    async def _ok_submit(*_args, **_kwargs):
        return {"ok": True, "report_id": "rpt_1", "duplicate": False}

    monkeypatch.setattr("app.api.routes.ugc_report_submit", _ok_submit)
    with TestClient(app) as client:
        res = client.post("/api/user-reports", json=_payload("valid"))
    assert res.status_code == 200
    success_keys = [k for k in fake.store.keys() if k.startswith("quota:user_reports:successful:")]
    assert len(success_keys) == 1
    assert int(fake.store[success_keys[0]]) == 1


def test_turnstile_failure_does_not_increment_success_quota(monkeypatch):
    fake = FakeRedis()
    app.state.redis = fake
    async def _invalid_turnstile(*_args, **_kwargs):
        return False

    monkeypatch.setattr("app.api.routes.verify_turnstile", _invalid_turnstile)
    with TestClient(app) as client:
        res = client.post("/api/user-reports", json=_payload("bad"))
    assert res.status_code == 403
    assert not any(k.startswith("quota:user_reports:successful:") for k in fake.store.keys())


def test_validation_failure_does_not_increment_success_quota():
    fake = FakeRedis()
    app.state.redis = fake
    with TestClient(app) as client:
        res = client.post("/api/user-reports", json={"lon": 108.244})
    assert res.status_code in (400, 422)
    assert not any(k.startswith("quota:user_reports:successful:") for k in fake.store.keys())


def test_db_insert_failure_does_not_increment_success_quota(monkeypatch):
    fake = FakeRedis()
    app.state.redis = fake

    async def _valid_turnstile(*_args, **_kwargs):
        return True

    async def _boom_submit(*_args, **_kwargs):
        raise RuntimeError("db insert failed")

    monkeypatch.setattr("app.api.routes.verify_turnstile", _valid_turnstile)
    monkeypatch.setattr("app.api.routes.ugc_report_submit", _boom_submit)
    with TestClient(app) as client:
        res = client.post("/api/user-reports", json=_payload("valid"))
    assert res.status_code == 500
    assert not any(k.startswith("quota:user_reports:successful:") for k in fake.store.keys())


def test_failed_attempts_before_valid_do_not_block(monkeypatch):
    fake = FakeRedis()
    app.state.redis = fake

    async def _valid_turnstile(*_args, **_kwargs):
        return True

    async def _ok_submit(*_args, **_kwargs):
        return {"ok": True, "report_id": "rpt_after_failures", "duplicate": False}

    monkeypatch.setattr("app.api.routes.verify_turnstile", _valid_turnstile)
    monkeypatch.setattr("app.api.routes.ugc_report_submit", _ok_submit)
    with TestClient(app) as client:
        bad_validation = client.post("/api/user-reports", json={"lat": 1})
        assert bad_validation.status_code in (400, 422)
        bad_turnstile = client.post("/api/user-reports", json=_payload(None))
        assert bad_turnstile.status_code == 403
        success = client.post("/api/user-reports", json=_payload("valid"))
    assert success.status_code == 200
    success_keys = [k for k in fake.store.keys() if k.startswith("quota:user_reports:successful:")]
    assert len(success_keys) == 1
    assert int(fake.store[success_keys[0]]) == 1


def test_search_quota_does_not_collide_with_report_quota(monkeypatch):
    fake = FakeRedis()
    app.state.redis = fake
    fake.store["dd:quota:anon:someanon:rolling24h"] = 999

    async def _valid_turnstile(*_args, **_kwargs):
        return True

    async def _ok_submit(*_args, **_kwargs):
        return {"ok": True, "report_id": "rpt_search_independent", "duplicate": False}

    monkeypatch.setattr("app.api.routes.verify_turnstile", _valid_turnstile)
    monkeypatch.setattr("app.api.routes.ugc_report_submit", _ok_submit)
    with TestClient(app) as client:
        res = client.post("/api/user-reports", json=_payload("valid"))
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "report_created"


def test_double_submit_duplicate_only_increments_once(monkeypatch):
    fake = FakeRedis()
    app.state.redis = fake

    async def _valid_turnstile(*_args, **_kwargs):
        return True

    responses = [{"ok": True, "report_id": "rpt_1", "duplicate": False}, {"ok": True, "report_id": "rpt_1", "duplicate": True}]

    async def _submit(*_args, **_kwargs):
        return responses.pop(0)

    monkeypatch.setattr("app.api.routes.verify_turnstile", _valid_turnstile)
    monkeypatch.setattr("app.api.routes.ugc_report_submit", _submit)
    with TestClient(app) as client:
        first = client.post("/api/user-reports", json=_payload("valid"))
        second = client.post("/api/user-reports", json=_payload("valid"))
    assert first.status_code == 200
    assert second.status_code == 200
    success_keys = [k for k in fake.store.keys() if k.startswith("quota:user_reports:successful:")]
    assert int(fake.store[success_keys[0]]) == 1


def test_dedup_ghost_bug_no_phantom_duplicate_after_failed_insert_then_retry(monkeypatch):
    fake = FakeRedis()
    app.state.redis = fake

    async def _valid_turnstile(*_args, **_kwargs):
        return True

    responses = [RuntimeError("db insert failed"), {"ok": True, "report_id": "rpt_retry_ok", "duplicate": False}]

    async def _submit(*_args, **_kwargs):
        nxt = responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr("app.api.routes.verify_turnstile", _valid_turnstile)
    monkeypatch.setattr("app.api.routes.ugc_report_submit", _submit)
    with TestClient(app) as client:
        first = client.post("/api/user-reports", json=_payload("valid"))
        second = client.post("/api/user-reports", json=_payload("valid"))
    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json()["status"] == "report_created"


def test_ugc_report_submit_does_not_increment_success_quota_on_db_failure(monkeypatch):
    fake = FakeRedis()
    app.state.redis = fake

    class _FailingConn:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("forced db insert failure")

    class _FailingBegin:
        async def __aenter__(self):
            return _FailingConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _FailingEngine:
        def begin(self):
            return _FailingBegin()

    app.state.db_engine = _FailingEngine()

    async def _valid_turnstile(*_args, **_kwargs):
        return True

    monkeypatch.setattr("app.api.routes.verify_turnstile", _valid_turnstile)

    with TestClient(app) as client:
        res = client.post("/api/user-reports", json=_payload("valid"))

    assert res.status_code == 503
    assert not any(k.startswith("quota:user_reports:successful:") for k in fake.store.keys())
