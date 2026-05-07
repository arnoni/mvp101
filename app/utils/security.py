# Implements TSD FR-002: Cloudflare Turnstile Verification
# Implements TSD Section 4.4: Business Logic (Verify Turnstile)
# Implements TSD Section 8: Validate and sanitize all inputs

from fastapi import Request, HTTPException, status
import hashlib
import httpx
import structlog
import sentry_sdk
from app.core.config import settings
from app.models.dto import ErrorResponse
from app.core.keys import KeyBuilder
from pydantic import validate_call
logger = structlog.get_logger(__name__)


def _safe_log(level: str, event: str, **fields) -> None:
    """Best-effort structlog event emission with Sentry fallback on logger failures."""
    try:
        getattr(logger, level)(event, **fields)
    except Exception as log_error:
        _capture_turnstile_issue(
            "turnstile_logging_failed",
            level="error",
            log_level=level,
            event_name=event,
            error_class=log_error.__class__.__name__,
            error_detail=str(log_error),
            log_fields=fields,
        )
        try:
            getattr(logger, level)(f"{event} | {fields}")
        except Exception as fallback_error:
            _capture_turnstile_issue(
                "turnstile_logging_fallback_failed",
                level="error",
                log_level=level,
                event_name=event,
                error_class=fallback_error.__class__.__name__,
                error_detail=str(fallback_error),
            )

try:
    from app.services.redis_client import redis_client
    if redis_client and redis_client.client:
        logger.info("Security utils: Redis client found and ready.")
    else:
        logger.warning("Security utils: Redis client correctly initialized as None (Disabled).")
except Exception as e:
    logger.error(f"Security utils: Failed to import redis_client: {e}")
    try:
        sentry_sdk.capture_exception(e)
    except Exception:
        pass
    redis_client = None



def _capture_turnstile_issue(event: str, *, level: str = "warning", **context) -> None:
    try:
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("component", "turnstile")
            scope.set_tag("event", event)
            for key, value in context.items():
                scope.set_extra(key, value)
            sentry_sdk.capture_message(event, level=level)
    except Exception:
        # Never let observability break auth/verification flow
        pass
async def protect_mutation(request: Request):
    # A. Enforce JSON only
    ct = request.headers.get("content-type", "")
    if "application/json" not in ct:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid content-type")
    # B. Enforce Origin (if configured)
    origin = request.headers.get("origin")
    if settings.APP_ORIGIN and origin:
        normalized_origin = origin.strip().rstrip("/")
        allowed_origins = [o.strip().rstrip("/") for o in settings.APP_ORIGIN.split(",") if o.strip()]
        preview_suffix = "-arnonis-projects.vercel.app"
        is_preview_origin = settings.ENV == "preview" and normalized_origin.startswith("https://") and normalized_origin.endswith(preview_suffix)
        if normalized_origin not in allowed_origins and not is_preview_origin:
            log_data = {
                "origin": origin,
                "normalized_origin": normalized_origin,
                "allowed": allowed_origins,
                "env": settings.ENV,
                "preview_suffix": preview_suffix,
            }
            logger.warning(f"Origin not allowed: {log_data}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origin not allowed")

    return True
TURNSTILE_SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_CACHE_TTL_SECONDS = 5 * 60


def _turnstile_ip_ua_hash(client_ip: str | None, user_agent: str | None) -> str:
    raw = f"{client_ip or 'unknown_ip'}:{user_agent or 'unknown_ua'}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _turnstile_cache_key(
    *,
    anon_id: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> str | None:
    if anon_id:
        return KeyBuilder.turnstile_verified(anon_id)
    if client_ip:
        return KeyBuilder.turnstile_verified_ip_ua(_turnstile_ip_ua_hash(client_ip, user_agent))
    return None


async def _read_turnstile_cache(cache_key: str | None) -> bool:
    if not cache_key or not redis_client:
        return False
    try:
        cached = await redis_client.get(cache_key)
    except Exception as exc:
        _safe_log(
            "error",
            "turnstile_cache_read_failed",
            cache_key=cache_key,
            redis_op="turnstile_replay_guard",
            error_class=exc.__class__.__name__,
            error_detail=str(exc),
        )
        return False
    if cached:
        logger.info("turnstile_verification_cache_hit", cache_key=cache_key, redis_op="turnstile_replay_guard")
        return True
    return False


async def _write_turnstile_cache(cache_key: str | None) -> None:
    if not cache_key or not redis_client:
        return
    try:
        await redis_client.setex(cache_key, TURNSTILE_CACHE_TTL_SECONDS, "1")
        logger.info("turnstile_verification_cache_written", cache_key=cache_key, redis_op="turnstile_replay_guard")
    except Exception as exc:
        _safe_log(
            "error",
            "turnstile_cache_write_failed",
            cache_key=cache_key,
            redis_op="turnstile_replay_guard",
            error_class=exc.__class__.__name__,
            error_detail=str(exc),
        )


@validate_call
async def verify_turnstile(
    token: str,
    client_ip: str | None = None,
    anon_id: str | None = None,
    *,
    user_agent: str | None = None,
    source: str | None = None,
    origin: str | None = None,
    hostname: str | None = None,
) -> bool:
    """
    Verify a Cloudflare Turnstile token with Cloudflare's server-side API.

    Successful validations are cached for five minutes by anonymous identity
    when available. If identity middleware has not supplied an anon_id yet, the
    cache falls back to a hash of client IP plus user-agent to reduce shared NAT
    collisions. Smoke-test tokens are intentionally never accepted here.
    """
    token = (token or "").strip()
    if not token:
        _safe_log("warning", "turnstile_token_missing", source=source, origin=origin, hostname=hostname)
        return False

    secret = settings.CLOUDFLARE_TURNSTILE_SECRET
    if not secret:
        _safe_log("error", "turnstile_secret_missing", source=source, origin=origin, hostname=hostname)
        return False

    cache_key = _turnstile_cache_key(anon_id=anon_id, client_ip=client_ip, user_agent=user_agent)
    if await _read_turnstile_cache(cache_key):
        return True

    data = {
        "secret": secret,
        "response": token,
    }
    if client_ip:
        data["remoteip"] = client_ip

    logger.info("turnstile_siteverify_started", source=source, origin=origin, hostname=hostname)
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(TURNSTILE_SITEVERIFY_URL, data=data)
            response.raise_for_status()
            result = response.json()
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        _safe_log(
            "error",
            "turnstile_api_unavailable",
            error_class=exc.__class__.__name__,
            error_detail=str(exc),
            source=source,
            origin=origin,
            hostname=hostname,
            client_ip=client_ip,
        )
        _capture_turnstile_issue(
            "turnstile_api_unavailable",
            level="error",
            error_class=exc.__class__.__name__,
            error_detail=str(exc),
            source=source,
            origin=origin,
            hostname=hostname,
            client_ip=client_ip,
        )
        return False
    except httpx.HTTPStatusError as exc:
        _safe_log(
            "error",
            "turnstile_api_unavailable",
            status_code=exc.response.status_code if exc.response else None,
            source=source,
            origin=origin,
            hostname=hostname,
            client_ip=client_ip,
        )
        _capture_turnstile_issue(
            "turnstile_api_unavailable",
            level="error",
            status_code=exc.response.status_code if exc.response else None,
            source=source,
            origin=origin,
            hostname=hostname,
            client_ip=client_ip,
        )
        return False
    except Exception as exc:
        _safe_log(
            "error",
            "turnstile_api_unavailable",
            error_class=exc.__class__.__name__,
            error_detail=str(exc),
            source=source,
            origin=origin,
            hostname=hostname,
            client_ip=client_ip,
        )
        _capture_turnstile_issue(
            "turnstile_api_unavailable",
            level="error",
            error_class=exc.__class__.__name__,
            error_detail=str(exc),
            source=source,
            origin=origin,
            hostname=hostname,
            client_ip=client_ip,
        )
        return False

    if result.get("success") is True:
        if settings.ENV == "preview":
            verified_hostname = (result.get("hostname") or "").strip()
            if not verified_hostname.endswith(settings.TURNSTILE_PREVIEW_HOSTNAME_SUFFIX):
                raise HTTPException(status_code=403, detail="Invalid Turnstile hostname")
        await _write_turnstile_cache(cache_key)
        logger.info("turnstile_siteverify_success", source=source, origin=origin, hostname=hostname)
        return True

    error_codes = result.get("error-codes", [])
    _safe_log(
        "warning",
        "turnstile_verification_failed",
        cloudflare_error_codes=error_codes,
        source=source,
        origin=origin,
        hostname=hostname,
        client_ip=client_ip,
    )
    _capture_turnstile_issue(
        "turnstile_verification_failed",
        cloudflare_error_codes=error_codes,
        source=source,
        origin=origin,
        hostname=hostname,
        client_ip=client_ip,
    )
    return False

async def is_turnstile_verified(
    anon_id: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> bool:
    """Checks if the user has already successfully verified Turnstile recently."""
    return await _read_turnstile_cache(
        _turnstile_cache_key(anon_id=anon_id, client_ip=client_ip, user_agent=user_agent)
    )


async def verify_turnstile_dependency(request: Request) -> bool:
    """FastAPI dependency that validates a Turnstile token or returns 403."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    token = None
    if isinstance(payload, dict):
        token = payload.get("turnstile_token") or payload.get("cf_turnstile_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorResponse(
                error="TURNSTILE_REQUIRED",
                detail="Human verification required.",
                error_id=getattr(request.state, "request_id", None),
            ).model_dump(),
        )

    ok = await verify_turnstile(
        token=token,
        client_ip=get_client_ip(request),
        anon_id=getattr(request.state, "anon_id", None),
        user_agent=request.headers.get("user-agent"),
        source=request.url.path if request.url else None,
        origin=request.headers.get("origin"),
        hostname=request.url.hostname if request.url else None,
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ErrorResponse(
                error="TURNSTILE_INVALID",
                detail="Verification failed. Please complete the human check again and resubmit.",
                error_id=getattr(request.state, "request_id", None),
            ).model_dump(),
        )
    return True

def get_client_ip(request: Request) -> str:
    """
    Extracts the client's IP address from the request.
    Assumes a standard proxy setup (e.g., Vercel/Render) where the
    client IP is in the 'x-forwarded-for' header.
    """
    # Implements TSD FR-003: Rate Limiting (1 req/IP/24h)
    # This is a critical security/cost control point.
    
    # Cloudflare canonical client IP header
    cf_connecting_ip = request.headers.get("cf-connecting-ip")
    if cf_connecting_ip:
        return cf_connecting_ip.strip()

    # Check for common proxy headers
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        # The first IP is the client's IP
        return x_forwarded_for.split(',')[0].strip()
    
    # Fallback to direct client host
    return request.client.host if request.client else "unknown_ip"
