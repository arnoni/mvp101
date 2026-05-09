"""Startup security validation helpers."""

import structlog

from app.core.config import Settings

logger = structlog.get_logger(__name__)


def validate_startup_security_settings(settings: Settings) -> None:
    """Refuse to start when unsafe security settings are present."""
    vercel_env = (settings.VERCEL_ENV or "").strip().lower()
    app_env = (settings.ENV or "").strip().lower()

    if vercel_env and app_env and vercel_env != app_env:
        logger.warning(
            "production_environment_source_disagreement",
            vercel_env=vercel_env,
            app_env=app_env,
            is_production=settings.is_production,
        )

    if (vercel_env == "preview") != (app_env == "preview"):
        logger.warning(
            "env_vercel_env_mismatch",
            vercel_env=vercel_env,
            app_env=app_env,
            is_production=settings.is_production,
        )

    if not settings.CLOUDFLARE_TURNSTILE_SECRET:
        logger.warning(
            "turnstile_secret_missing_at_startup",
            env=settings.ENV,
            vercel_env=settings.VERCEL_ENV,
        )

    if settings.is_production and settings.SMOKE_TURNSTILE_TOKEN:
        logger.critical(
            "unsafe_production_config",
            unsafe_variable="SMOKE_TURNSTILE_TOKEN",
            action="refusing_startup",
        )
        raise RuntimeError("SMOKE_TURNSTILE_TOKEN must not be set in production")

    if settings.SMOKE_TURNSTILE_TOKEN:
        logger.warning(
            "smoke_turnstile_token_configured_non_production",
            env=settings.ENV,
            vercel_env=settings.VERCEL_ENV,
        )
