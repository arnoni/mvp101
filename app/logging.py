import logging
import sys

import structlog

from app.core.config import settings

SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "dsn",
    "poi_dist_m",
    "poi_lats",
    "poi_lons",
    "poi_ids",
    "distances_user_m",
    "distance_m",
    "lat",
    "lon",
    "raw_lat",
    "raw_lon",
    "user_lat",
    "user_lon",
}


def scrub_sensitive_fields(logger, method_name, event_dict):
    """Redact sensitive fields before Structlog emits them to Vercel/stdout."""
    def scrub(obj):
        if isinstance(obj, dict):
            safe = {}
            for key, value in obj.items():
                key_text = str(key).lower()
                if any(sensitive in key_text for sensitive in SENSITIVE_KEYS):
                    safe[key] = "<redacted>"
                else:
                    safe[key] = scrub(value)
            return safe
        if isinstance(obj, list):
            if len(obj) > 5 and all(isinstance(x, (int, float)) for x in obj):
                return ["<redacted_array>"]
            return [scrub(x) for x in obj]
        return obj

    return scrub(event_dict)


def configure_logging():
    """
    Configures structlog to intercept standard library logs and setup
    JSON rendering for production or Console rendering for local development.
    """
    
    # Determine if we are in local dev or production
    # settings.ENV is 'development' by default in config.py
    is_local = settings.ENV.lower() == "development"

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            }
        ),
        scrub_sensitive_fields,
    ]

    if is_local:
        # Human-readable for local development
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer()
        ]
    else:
        # JSON for production
        processors = shared_processors + [
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer()
        ]

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Intercept standard library logging
    # We set the level to INFO to capture most things, but you can adjust.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

    # Reconfigure uvicorn loggers to use structlog
    # This ensures uvicorn access logs and errors are formatted consistently
    for _log in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        logger = logging.getLogger(_log)
        logger.handlers = [] # Clear existing handlers
        logger.propagate = True # Let it propagate to the root logger we just configured
