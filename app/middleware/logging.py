import time
import uuid
from collections.abc import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.core.observability import bind_request_to_sentry, get_vercel_context, report_exception

log = structlog.get_logger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        clear_contextvars()

        request_id = str(uuid.uuid4())
        vercel_id = request.headers.get("x-vercel-id")
        cf_ray = request.headers.get("cf-ray")
        vercel_context = get_vercel_context()

        bind_contextvars(
            request_id=request_id,
            http_method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else "unknown",
            vercel_id=vercel_id,
            cf_ray=cf_ray,
            **vercel_context,
        )
        bind_request_to_sentry(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else None,
            vercel_id=vercel_id,
        )

        request.state.request_id = request_id
        request.state.vercel_id = vercel_id

        start_time = time.perf_counter()

        try:
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            if response.status_code >= 500:
                log.error(
                    "http_request_5xx_response",
                    status_code=response.status_code,
                    elapsed_ms=round(elapsed_ms, 2),
                )
            else:
                log.info(
                    "http_request",
                    status_code=response.status_code,
                    elapsed_ms=round(elapsed_ms, 2),
                )

            response.headers["X-Request-ID"] = request_id
            if vercel_id:
                response.headers["X-Vercel-ID"] = vercel_id

            return response

        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            request.state.error_reported = True
            report_exception(
                exc,
                event="http_request_failed",
                logger=log,
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                elapsed_ms=round(elapsed_ms, 2),
                status_code=500,
                vercel_id=vercel_id,
                cf_ray=cf_ray,
            )
            raise
        finally:
            clear_contextvars()
