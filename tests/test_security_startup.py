import pytest

from app.core.config import get_settings
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

    validate_startup_security_settings(get_settings())
