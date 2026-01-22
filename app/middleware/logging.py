import time
import uuid
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

log = structlog.get_logger()

class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        clear_contextvars()
        
        request_id = str(uuid.uuid4())
        
        # Bind context variables for this request
        bind_contextvars(
            request_id=request_id,
            http_method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else "unknown",
        )

        # Inject request_id into request state for Jinja2 templates
        request.state.request_id = request_id

        start_time = time.perf_counter()
        
        try:
            response = await call_next(request)
            
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            
            # Log the successful request
            log.info(
                "http_request",
                status_code=response.status_code,
                elapsed_ms=round(elapsed_ms, 2)
            )
            
            # Add request ID to the response headers
            response.headers["X-Request-ID"] = request_id
            
            return response
            
        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            
            # Log the exception with full traceback
            log.exception(
                "http_request_failed",
                elapsed_ms=round(elapsed_ms, 2),
                error=str(e)
            )
            # Re-raise so FastAPI's exception handler can catch it or return 500
            raise e
