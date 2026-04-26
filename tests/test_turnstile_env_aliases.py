from app.core.config import Settings


def test_turnstile_legacy_env_aliases_are_supported(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "legacy-site")
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "legacy-secret")
    monkeypatch.delenv("CLOUDFLARE_TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_TURNSTILE_SECRET", raising=False)

    settings = Settings()

    assert settings.CLOUDFLARE_TURNSTILE_SITE_KEY == "legacy-site"
    assert settings.CLOUDFLARE_TURNSTILE_SECRET == "legacy-secret"
