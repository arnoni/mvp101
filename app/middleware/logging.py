import time
import uuid
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
import sentry_sdk  # <--- IMPORT THIS

log = structlog.get_logger()

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        clear_contextvars()
        
        request_id = str(uuid.uuid4())
        
        # 1. Bind context for Structlog
        bind_contextvars(
            request_id=request_id,
            http_method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else "unknown",
        )

        # 2. Bind context for Sentry (THE IMPROVEMENT)
        # Now you can search "request_id:abc-123" in Sentry to find the crash
        sentry_sdk.set_tag("request_id", request_id)

        # Inject request_id into request state for Jinja2 templates
        request.state.request_id = request_id

        start_time = time.perf_counter()
        
        try:
            response = await call_next(request)
            
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            
            log.info(
                "http_request",
                status_code=response.status_code,
                elapsed_ms=round(elapsed_ms, 2)
            )
            
            response.headers["X-Request-ID"] = request_id
            
            return response
            
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            
            # Log to console/file
            log.exception(
                "http_request_failed",
                elapsed_ms=round(elapsed_ms, 2),
                error=str(e)
            )
            
            # 3. DO NOT manually capture_exception(e) here!
            # Since you are re-raising below, Sentry's global integration 
            # will catch it automatically. Doing it here would cause double-reporting.
            
            raise e