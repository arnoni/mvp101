from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.utils.rate_limits import (
    RateLimitBackendError,
    RateLimitIdentity,
    RateLimitResult,
    check_rate_limits,
    denied_result,
    hash_rate_limit_identity,
    normalize_email,
)
from app.utils.security import get_client_ip


class AtomicRateLimitRedis:
    def __init__(self):
        self.now = 0
        self.store: dict[str, int] = {}
        self.expires_at: dict[str, int] = {}
        self.keys_seen: list[str] = []
        self.lock = asyncio.Lock()

    async def eval(self, _script: str, keys: list[str], args: list[int]):
        async with self.lock:
            key = keys[0]
            limit, ttl = args
            self.keys_seen.append(key)
            if self.expires_at.get(key, 0) <= self.now:
                self.store.pop(key, None)
                self.expires_at.pop(key, None)
            current = self.store.get(key, 0) + 1
            self.store[key] = current
            if current == 1:
                self.expires_at[key] = self.now + ttl
            remaining_ttl = max(1, self.expires_at[key] - self.now)
            return [current, int(current <= limit), remaining_ttl]

    def advance(self, seconds: int) -> None:
        self.now += seconds


def _request(
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 1234),
) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": headers or [],
            "client": client,
            "app": app,
            "state": {},
        }
    )


def test_atomic_fixed_window_threshold_ttl_retry_and_reset():
    async def run():
        redis = AtomicRateLimitRedis()
        identity = RateLimitIdentity("email", "email", "person@example.com")
        outcomes = []
        for _ in range(4):
            outcomes.append(
                (
                    await check_rate_limits(
                        redis,
                        route="/api/auth/magic-link",
                        identities=[identity],
                        limit=3,
                        window_seconds=60,
                        now=0,
                    )
                )[0]
            )

        assert [result.allowed for result in outcomes] == [True, True, True, False]
        assert outcomes[-1].retry_after_seconds > 0
        key = redis.keys_seen[0]
        assert redis.expires_at[key] == 60

        redis.advance(60)
        reset = (
            await check_rate_limits(
                redis,
                route="/api/auth/magic-link",
                identities=[identity],
                limit=3,
                window_seconds=60,
                now=60,
            )
        )[0]
        assert reset.allowed is True
        assert reset.current_count == 1

    asyncio.run(run())


def test_route_and_dimension_keys_are_independent_and_opaque():
    async def run():
        redis = AtomicRateLimitRedis()
        raw_email = "Person@Example.com"
        raw_ip = "203.0.113.7"
        await check_rate_limits(
            redis,
            route="/api/search",
            identities=[
                RateLimitIdentity("email", "email", normalize_email(raw_email)),
                RateLimitIdentity("ip", "ip", raw_ip),
            ],
            limit=2,
            window_seconds=60,
            now=0,
        )
        await check_rate_limits(
            redis,
            route="/api/telemetry/client-event",
            identities=[RateLimitIdentity("ip", "ip", raw_ip)],
            limit=2,
            window_seconds=60,
            now=0,
        )

        assert len(set(redis.keys_seen)) == 3
        assert all(key.startswith("dd:ratelimit:v1:") for key in redis.keys_seen)
        assert all(raw_email not in key for key in redis.keys_seen)
        assert all(raw_ip not in key for key in redis.keys_seen)

    asyncio.run(run())


def test_email_normalization_maps_case_and_whitespace_to_same_hash():
    assert hash_rate_limit_identity(
        "email", normalize_email("  Person@Example.com ")
    ) == hash_rate_limit_identity("email", normalize_email("person@example.com"))


def test_all_dimensions_increment_before_denial_is_evaluated():
    async def run():
        redis = AtomicRateLimitRedis()
        identities = [
            RateLimitIdentity("identity", "anon", "anon-1"),
            RateLimitIdentity("ip", "ip", "203.0.113.8"),
        ]
        await check_rate_limits(
            redis,
            route="/api/search",
            identities=identities,
            limit=1,
            window_seconds=60,
            now=0,
        )
        second = await check_rate_limits(
            redis,
            route="/api/search",
            identities=identities,
            limit=1,
            window_seconds=60,
            now=0,
        )
        assert denied_result(second) is not None
        assert [result.current_count for result in second] == [2, 2]

    asyncio.run(run())


def test_missing_backend_raises_explicit_error():
    async def run():
        with pytest.raises(RateLimitBackendError):
            await check_rate_limits(
                None,
                route="/api/search",
                identities=[RateLimitIdentity("identity", "anon", "anon-1")],
                limit=1,
                window_seconds=60,
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("header", "direct", "expected"),
    [
        (b"garbage, also-bad", ("not-an-ip", 1), None),
        (b"198.51.100.1, 203.0.113.9", ("127.0.0.1", 1), "203.0.113.9"),
        (b"2001:db8::1", ("127.0.0.1", 1), "2001:db8::1"),
    ],
)
def test_client_ip_validation(header, direct, expected):
    request = _request(headers=[(b"x-forwarded-for", header)], client=direct)
    assert get_client_ip(request) == expected


def test_missing_ip_returns_none_not_shared_unknown_bucket():
    assert get_client_ip(_request(client=None)) is None


def test_search_backend_failure_bypasses_in_non_production(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RateLimitBackendError(dimension="identity", error=RuntimeError("down"))

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.routes.check_rate_limits", fail)
    monkeypatch.setattr("app.api.routes.protect_mutation", noop)
    monkeypatch.setattr(settings, "ENV", "development")
    monkeypatch.setattr(settings, "VERCEL_ENV", None)
    with TestClient(app) as client:
        response = client.post("/api/search", json={"lat": 16.02, "lon": 108.25})
    assert response.status_code != 503


def test_search_backend_failure_fails_closed_in_production(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RateLimitBackendError(dimension="identity", error=RuntimeError("down"))

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.routes.check_rate_limits", fail)
    monkeypatch.setattr("app.api.routes.protect_mutation", noop)
    monkeypatch.setattr(settings, "ENV", "production")
    monkeypatch.setattr(settings, "VERCEL_ENV", "production")
    monkeypatch.setattr(settings, "RATE_LIMIT_HMAC_SECRET", "production-rate-limit-secret")
    with TestClient(app) as client:
        response = client.post("/api/search", json={"lat": 16.02, "lon": 108.25})
    assert response.status_code == 503
    assert response.json()["error"] == "RATE_LIMIT_UNAVAILABLE"


def test_telemetry_backend_failure_returns_success_without_payload_log(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RateLimitBackendError(dimension="identity", error=RuntimeError("down"))

    async def noop(*_args, **_kwargs):
        return None

    payload_logs: list[tuple] = []
    monkeypatch.setattr("app.api.routes.check_rate_limits", fail)
    monkeypatch.setattr("app.api.routes.protect_mutation", noop)
    monkeypatch.setattr(
        "app.api.routes.logger",
        SimpleNamespace(
            info=lambda *args, **kwargs: payload_logs.append((args, kwargs)),
            error=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
        ),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/telemetry/client-event",
            json={"event": "client_test_event"},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert payload_logs == []


def test_magic_link_backend_failure_preserves_generic_response(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RateLimitBackendError(dimension="ip", error=RuntimeError("down"))

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.auth.check_rate_limits", fail)
    monkeypatch.setattr("app.api.auth.protect_mutation", noop)
    monkeypatch.setattr("app.api.auth.get_client_ip", lambda _request: "203.0.113.20")
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/magic-link",
            json={"email": "eligible@example.com", "turnstile_token": "token"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "message": "If this email is eligible, we sent a new access link."
    }


def test_unlock_backend_failure_fails_closed_before_turnstile(monkeypatch):
    async def fail(*_args, **_kwargs):
        raise RateLimitBackendError(dimension="identity", error=RuntimeError("down"))

    async def noop(*_args, **_kwargs):
        return None

    async def turnstile_must_not_run(*_args, **_kwargs):
        raise AssertionError("Turnstile must not run after limiter backend failure")

    monkeypatch.setattr("app.api.billing.check_rate_limits", fail)
    monkeypatch.setattr("app.api.billing.protect_mutation", noop)
    monkeypatch.setattr("app.api.billing.verify_turnstile", turnstile_must_not_run)
    with TestClient(app) as client:
        response = client.post(
            "/api/billing/unlock-intent",
            json={"email": "person@example.com", "plan": "sim_1_day", "turnstile_token": "token"},
        )
    assert response.status_code == 503


def _denied(route: str, dimension: str = "identity") -> list[RateLimitResult]:
    return [
        RateLimitResult(
            route=route,
            dimension=dimension,
            identity_kind="anon",
            identity_hash="a" * 32,
            limit=1,
            window_seconds=60,
            current_count=2,
            allowed=False,
            retry_after_seconds=42,
        )
    ]


@pytest.mark.parametrize(
    ("path", "payload", "module", "route"),
    [
        (
            "/api/search",
            {"lat": 16.02, "lon": 108.25},
            "app.api.routes",
            "/api/search",
        ),
        (
            "/api/auth/magic-link",
            {"email": "person@example.com", "turnstile_token": "token"},
            "app.api.auth",
            "/api/auth/magic-link",
        ),
        (
            "/api/billing/unlock-intent",
            {"email": "person@example.com", "plan": "sim_1_day", "turnstile_token": "token"},
            "app.api.billing",
            "/api/billing/unlock-intent",
        ),
        (
            "/api/telemetry/client-event",
            {"event": "client_test_event"},
            "app.api.routes",
            "/api/telemetry/client-event",
        ),
    ],
)
def test_endpoint_denials_return_429_retry_after_and_request_id(
    monkeypatch, path, payload, module, route
):
    async def deny(*_args, **_kwargs):
        return _denied(route)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(f"{module}.check_rate_limits", deny)
    monkeypatch.setattr(f"{module}.protect_mutation", noop)
    if module in {"app.api.auth", "app.api.billing"}:
        monkeypatch.setattr(f"{module}.get_client_ip", lambda _request: "203.0.113.20")

    with TestClient(app) as client:
        response = client.post(path, json=payload)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    assert response.headers["X-Request-ID"]
    assert response.json()["error"] == "RATE_LIMITED"


def test_magic_landing_get_is_not_abuse_rate_limited(monkeypatch):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("landing route must not call abuse limiter")

    monkeypatch.setattr("app.api.auth.check_rate_limits", fail_if_called)
    with TestClient(app) as client:
        response = client.get("/api/auth/magic")
    assert response.status_code != 429
