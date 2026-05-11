import importlib
import importlib.util
import os

import sentry_sdk
import structlog

posthog = (
    importlib.import_module("posthog")
    if importlib.util.find_spec("posthog") is not None
    else None
)

log = structlog.get_logger()

_posthog_configured = False


def configure_posthog() -> None:
    """Configure the PostHog module once, after environment variables are available."""
    global _posthog_configured
    if _posthog_configured or posthog is None:
        return

    posthog.project_api_key = os.environ.get("POSTHOG_PROJECT_API_KEY", "")
    posthog.host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
    _posthog_configured = True


def capture(user_id: str, event: str, properties: dict = None) -> None:
    """Send one event to PostHog. Always flushes immediately (Vercel serverless
    requirement — process exits after response, buffered events are dropped).
    Never raises — analytics must never abort business logic."""
    configure_posthog()
    if posthog is None or not getattr(posthog, "project_api_key", ""):
        return
    try:
        posthog.capture(
            distinct_id=str(user_id),
            event=event,
            properties=properties or {},
        )
        posthog.flush()
    except Exception as e:
        log.error(
            "posthog_capture_failed",
            event=event,
            error_type=type(e).__name__,
            error_detail=str(e),
        )
        sentry_sdk.capture_exception(e)


def capture_with_properties(
    user_id: str,
    event: str,
    properties: dict | None = None,
) -> None:
    """
    Alias for capture() — keeps naming consistent across the codebase.
    analytics.capture() is the single source of truth for PostHog events.
    """
    capture(user_id, event, properties)
