import os
import posthog
import structlog

log = structlog.get_logger()

posthog.project_api_key = os.environ.get("POSTHOG_PROJECT_API_KEY", "")
posthog.host = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")


def capture(user_id: str, event: str, properties: dict = None) -> None:
    """Send one event to PostHog. Always flushes immediately (Vercel serverless
    requirement — process exits after response, buffered events are dropped).
    Never raises — analytics must never abort business logic."""
    if not posthog.project_api_key:
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
