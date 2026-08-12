import logging
import os
from collections.abc import Mapping
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

SENTRY_TAG_VALUE_MAX_LENGTH = 200
SENTRY_TAG_KEY_MAX_LENGTH = 32

_SENSITIVE_CONTEXT_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "dsn",
}


def _safe_context_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(sensitive in key_text for sensitive in _SENSITIVE_CONTEXT_KEYS):
                safe[str(key)] = "<redacted>"
            else:
                safe[str(key)] = _safe_context_value(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_safe_context_value(item) for item in value[:20]]
    if isinstance(value, str) and len(value) > 1000:
        return f"{value[:1000]}…<truncated>"
    return value


def _bind_safe_context_to_scope(scope: Any, context: Mapping[str, Any]) -> None:
    """Attach sanitized context as Sentry tags/extras without allowing bad values to fail reporting."""
    for key, value in context.items():
        key_text = str(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            scope.set_tag(
                key_text[:SENTRY_TAG_KEY_MAX_LENGTH],
                str(value)[:SENTRY_TAG_VALUE_MAX_LENGTH],
            )
        scope.set_extra(key_text, value)


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Final Sentry scrubber for request/session contexts that may bypass our helpers."""
    return _safe_context_value(event)


def capture_message(
    event: str,
    *,
    level: str = "warning",
    logger: Any | None = None,
    **context: Any,
) -> None:
    """Report a non-exception failure path to logs and Sentry without breaking the caller."""
    safe_context = _safe_context_value({**get_vercel_context(), **context})
    log = logger or logging.getLogger(__name__)

    try:
        if hasattr(log, "bind"):
            getattr(
                log,
                level if level in {"debug", "info", "warning", "error", "critical"} else "warning",
            )(
                event,
                **safe_context,
            )
        else:
            getattr(
                log,
                level if level in {"debug", "info", "warning", "error", "critical"} else "warning",
            )(
                event,
                extra=safe_context,
            )
    except Exception:
        logging.getLogger(__name__).exception(
            "observability_structlog_message_failed",
            extra={"event": event, "level": level},
        )

    try:
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("event", event)
            scope.set_tag("reported_kind", "message")
            _bind_safe_context_to_scope(scope, safe_context)
            sentry_sdk.capture_message(event, level=level)
    except Exception:
        logging.getLogger(__name__).exception(
            "observability_sentry_message_failed",
            extra={"event": event, "level": level},
        )


def get_vercel_context() -> dict[str, str]:
    """Return deployment metadata that Vercel includes in function env vars."""
    keys = (
        "VERCEL",
        "VERCEL_ENV",
        "VERCEL_REGION",
        "VERCEL_URL",
        "VERCEL_DEPLOYMENT_ID",
        "VERCEL_GIT_COMMIT_SHA",
        "VERCEL_GIT_COMMIT_REF",
    )
    return {key.lower(): value for key in keys if (value := os.getenv(key))}


def init_sentry(dsn: str | None, env: str, release: str | None) -> None:
    """
    Initialize Sentry.
    Note: We do not return a middleware. Sentry SDK hooks into FastAPI automatically
    via the integrations argument.
    """
    log = logging.getLogger(__name__)
    if not dsn:
        log.info(
            "sentry_dsn_missing_skipping_initialization", extra={"vercel": get_vercel_context()}
        )
        return

    sentry_logging = LoggingIntegration(
        level=logging.INFO,  # Capture info and above as breadcrumbs
        event_level=logging.ERROR,  # Send errors as events
    )
    vercel_context = get_vercel_context()

    sentry_sdk.init(
        dsn=dsn,
        environment=env,
        release=release,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            sentry_logging,
        ],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")),
        send_default_pii=False,
        include_local_variables=False,
        attach_stacktrace=True,
        max_breadcrumbs=int(os.getenv("SENTRY_MAX_BREADCRUMBS", "100")),
        before_send=_before_send,
    )

    with sentry_sdk.configure_scope() as scope:
        for key, value in vercel_context.items():
            scope.set_tag(key, value)
        if vercel_context:
            scope.set_context("vercel", vercel_context)

    log.info(
        "sentry_initialized",
        extra={"environment": env, "release": release, "vercel": vercel_context},
    )


def bind_request_to_sentry(
    *,
    request_id: str,
    method: str,
    path: str,
    client_ip: str | None = None,
    vercel_id: str | None = None,
) -> None:
    """Attach non-PII request/deployment metadata to the current Sentry scope."""
    with sentry_sdk.configure_scope() as scope:
        scope.set_tag("request_id", request_id)
        scope.set_tag("http.method", method)
        scope.set_tag("http.route_path", path)
        if vercel_id:
            scope.set_tag("vercel.request_id", vercel_id)
        scope.set_context(
            "request_context",
            {
                "request_id": request_id,
                "method": method,
                "path": path,
                "client_ip_present": bool(client_ip),
                "vercel_id": vercel_id,
            },
        )


def report_exception(
    exc: BaseException,
    *,
    event: str,
    logger: Any | None = None,
    flush: bool = False,
    **context: Any,
) -> None:
    """Report an exception to Structlog/Vercel logs and Sentry without raising a new error."""
    safe_context = _safe_context_value({**get_vercel_context(), **context})
    log = logger or logging.getLogger(__name__)

    try:
        exc_info = (type(exc), exc, exc.__traceback__) if exc.__traceback__ else True
        if hasattr(log, "bind"):
            log.error(
                event,
                error_class=exc.__class__.__name__,
                error=str(exc),
                exc_info=exc_info,
                **safe_context,
            )
        else:
            log.error(
                event,
                exc_info=exc_info,
                extra={
                    "error_class": exc.__class__.__name__,
                    "error": str(exc),
                    **safe_context,
                },
            )
    except Exception:
        logging.getLogger(__name__).exception(
            "observability_structlog_report_failed",
            extra={"event": event, "error_class": exc.__class__.__name__},
        )

    try:
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("event", event)
            scope.set_tag("error_class", exc.__class__.__name__)
            _bind_safe_context_to_scope(scope, safe_context)
            sentry_sdk.capture_exception(exc)
            if flush:
                sentry_sdk.flush(timeout=2.0)
    except Exception:
        logging.getLogger(__name__).exception(
            "observability_sentry_report_failed",
            extra={"event": event, "error_class": exc.__class__.__name__},
        )
