import logging
import sys
import structlog
from app.core.config import settings

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
