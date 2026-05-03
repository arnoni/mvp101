# Implements TSD FR-002: Cloudflare Turnstile Verification
# Implements TSD Section 4.4: Business Logic (Verify Turnstile)
# Implements TSD Section 8: Validate and sanitize all inputs

from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
import httpx
import logging
import sentry_sdk
from app.core.config import settings
from app.models.dto import ErrorResponse
from pydantic import validate_call
logger = logging.getLogger(__name__)

try:
    from app.services.redis_client import redis_client
    if redis_client and redis_client.client:
        logger.info("Security utils: Redis client found and ready.")
    else:
        logger.warning("Security utils: Redis client correctly initialized as None (Disabled).")
except Exception as e:
    logger.error(f"Security utils: Failed to import redis_client: {e}")
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
@validate_call
async def verify_turnstile(token: str, client_ip: str | None = None, *, source: str | None = None, origin: str | None = None, hostname: str | None = None) -> bool:
    """
    Verifies the Cloudflare Turnstile token against the Cloudflare API.
    Implements TSD Section 6: Turnstile verification.
    """
    # 0. SMOKE TEST BYPASS: If token matches secret smoke token, skip verification
    if settings.SMOKE_TURNSTILE_TOKEN and token == settings.SMOKE_TURNSTILE_TOKEN:
        logger.warning("turnstile_smoke_bypass", client_ip=client_ip)
        return True

    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    data = {
        "secret": settings.CLOUDFLARE_TURNSTILE_SECRET,
        "response": token,
        "remoteip": client_ip
    }
    
    logger.info("Initiating Turnstile verification check")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(url, data=data)
            response.raise_for_status()
            result = response.json()
            
            if result.get("success"):
                if settings.ENV == "preview":
                    hostname = (result.get("hostname") or "").strip()
                    if not hostname.endswith(settings.TURNSTILE_PREVIEW_HOSTNAME_SUFFIX):
                        raise HTTPException(status_code=403, detail="Invalid Turnstile hostname")
                logger.info("Turnstile EXACT verification SUCCESS")
                return True
            else:
                error_codes = result.get("error-codes", [])
                logger.warning("turnstile_verification_failed", cloudflare_error_codes=error_codes, source=source, origin=origin, hostname=hostname, client_ip=client_ip)
                _capture_turnstile_issue("turnstile_verification_failed", cloudflare_error_codes=error_codes, source=source, origin=origin, hostname=hostname, client_ip=client_ip)
                return False
    except httpx.TimeoutException:
        # ... (error handling)
        logger.error("Turnstile verification timed out.")
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail=ErrorResponse(
                error="TURNSTILE_TIMEOUT",
                detail="Turnstile verification service timed out."
            ).model_dump()
        )
    except httpx.HTTPStatusError as e:
        logger.error("turnstile_siteverify_http_error", status_code=e.response.status_code if e.response else None, source=source, origin=origin, hostname=hostname, client_ip=client_ip)
        _capture_turnstile_issue("turnstile_siteverify_http_error", level="error", status_code=e.response.status_code if e.response else None, source=source, origin=origin, hostname=hostname, client_ip=client_ip)
        return False
    except Exception as e:
        logger.error(f"Unexpected error during Turnstile verification: {e}")
        _capture_turnstile_issue("turnstile_verification_unexpected_error", level="error", error_class=e.__class__.__name__, error_detail=str(e), source=source, origin=origin, hostname=hostname, client_ip=client_ip)
        return False

async def is_turnstile_verified(anon_id: str | None = None, client_ip: str | None = None) -> bool:
    """Checks if the user has already successfully verified Turnstile recently."""
    if not redis_client:
        return False
        
    cache_key = None
    if anon_id:
        cache_key = f"turnstile_ok:{anon_id}"
    elif client_ip:
        cache_key = f"turnstile_ok_ip:{client_ip}"
        
    if cache_key:
        try:
            val = await redis_client.get(cache_key)
            if val:
                logger.info(f"Turnstile challenge skipped (cached) for key: {cache_key}")
                return True
            else:
                logger.debug(f"Turnstile challenge required (no cache) for key: {cache_key}")
        except Exception as e:
            logger.error(f"Error checking turnstile cache: {e}")
    return False

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
