import structlog
import logging
import sys
from app.core.config import settings

SENSITIVE_KEYS = {
    "poi_dist_m", "poi_lats", "poi_lons", "poi_ids",
    "distances_user_m", "distance_m", "lat", "lon", 
    "raw_lat", "raw_lon", "user_lat", "user_lon"
}

def scrub_sensitive_fields(logger, method_name, event_dict):
    """Redact sensitive fields from logs"""
    def scrub(obj):
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items() if k not in SENSITIVE_KEYS}
        elif isinstance(obj, list):
            # Redact large float arrays (likely coordinates/distances)
            if len(obj) > 5 and all(isinstance(x, (int, float)) for x in obj):
                return ["<redacted_array>"]
            return [scrub(x) for x in obj]
        return obj
    
    return scrub(event_dict)

def configure_logging():
    """
    Configures structlog to intercept standard library logs and setup
    JSON rendering for production or Console rendering for local development.
    With added sensitive data scrubbing.
    """
    
    # Determine if we are in local dev or production
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
        scrub_sensitive_fields, # <--- NEW: Scrubber
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
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

    # Reconfigure uvicorn loggers to use structlog
    for _log in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        logger = logging.getLogger(_log)
        logger.handlers = [] # Clear existing handlers
        logger.propagate = True
