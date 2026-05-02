import os
import structlog

try:
    import posthog
except ModuleNotFoundError:  # Optional dependency in some deploy targets
    posthog = None

log = structlog.get_logger()

POSTHOG_PROJECT_API_KEY = os.environ.get("POSTHOG_PROJECT_API_KEY", "")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")

if posthog is not None:
    posthog.project_api_key = POSTHOG_PROJECT_API_KEY
    posthog.host = POSTHOG_HOST


def capture(user_id: str, event: str, properties: dict = None) -> None:
    """Send one event to PostHog. Always flushes immediately (Vercel serverless
    requirement — process exits after response, buffered events are dropped).
    Never raises — analytics must never abort business logic."""
    if posthog is None or not POSTHOG_PROJECT_API_KEY:
        return
    try:
        posthog.capture(
            distinct_id=str(user_id),
            event=event,
            properties=properties or {}
        )
        posthog.flush()
    except Exception as e:
        log.warning("posthog_capture_failed", event=event, error=str(e))
