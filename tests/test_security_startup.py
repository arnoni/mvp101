import pytest

from app.core.config import Settings, get_settings
from app.core.security_startup import validate_startup_security_settings


def test_smoke_turnstile_token_forbidden_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.setenv("SMOKE_TURNSTILE_TOKEN", "test-token")

    with pytest.raises(
        RuntimeError,
        match="SMOKE_TURNSTILE_TOKEN must not be set in production",
    ):
        validate_startup_security_settings(get_settings())


def test_smoke_turnstile_token_allowed_in_preview(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "preview")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    monkeypatch.setenv("SMOKE_TURNSTILE_TOKEN", "test-token")

    validate_startup_security_settings(get_settings())


def test_production_without_smoke_token_starts_cleanly(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.delenv("SMOKE_TURNSTILE_TOKEN", raising=False)
    monkeypatch.setenv("RATE_LIMIT_HMAC_SECRET", "production-rate-limit-secret")

    validate_startup_security_settings(get_settings())


def test_rate_limit_hmac_secret_required_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.delenv("RATE_LIMIT_HMAC_SECRET", raising=False)

    with pytest.raises(
        RuntimeError,
        match="RATE_LIMIT_HMAC_SECRET must be set in production",
    ):
        validate_startup_security_settings(get_settings())


def test_missing_turnstile_secret_warns_at_startup(monkeypatch):
    calls = []
    monkeypatch.setattr("app.core.security_startup.logger.warning", lambda event, **kwargs: calls.append((event, kwargs)))

    validate_startup_security_settings(Settings(CLOUDFLARE_TURNSTILE_SECRET=None))

    assert any(event == "turnstile_secret_missing_at_startup" for event, _kwargs in calls)


def test_preview_env_mismatch_warns_at_startup(monkeypatch):
    calls = []
    monkeypatch.setattr("app.core.security_startup.logger.warning", lambda event, **kwargs: calls.append((event, kwargs)))

    validate_startup_security_settings(
        Settings(
            ENV="production",
            VERCEL_ENV="preview",
            CLOUDFLARE_TURNSTILE_SECRET="secret",
            RATE_LIMIT_HMAC_SECRET="production-rate-limit-secret",
        )
    )

    assert any(event == "env_vercel_env_mismatch" for event, _kwargs in calls)
