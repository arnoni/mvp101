# Implements TSD FR-002: Cloudflare Turnstile Verification
# Implements TSD Section 4.4: Business Logic (Verify Turnstile)
# Implements TSD Section 8: Validate and sanitize all inputs

from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
import httpx
import logging
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

async def protect_mutation(request: Request):
    # A. Enforce JSON only
    ct = request.headers.get("content-type", "")
    if "application/json" not in ct:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid content-type")
    # B. Enforce Origin (if configured)
    origin = request.headers.get("origin")
    if settings.APP_ORIGIN and origin:
        allowed_origins = [o.strip().rstrip("/") for o in settings.APP_ORIGIN.split(",")]
        if origin.rstrip("/") not in allowed_origins:
            log_data = {"origin": origin, "allowed": allowed_origins}
            logger.warning(f"Origin not allowed: {log_data}")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origin not allowed")

    return True
@validate_call
async def verify_turnstile(token: str, anon_id: str | None = None, client_ip: str | None = None) -> bool:
    """
    Verifies the Cloudflare Turnstile token against the Cloudflare API.
    Implements TSD Section 6: Turnstile verification.
    """
    # 0. SMOKE TEST BYPASS: If token matches secret smoke token, skip verification
    if settings.SMOKE_TURNSTILE_TOKEN and token == settings.SMOKE_TURNSTILE_TOKEN:
        logger.info("SMOKE TEST BYPASS: Valid smoke token detected. Skipping Turnstile verification.")
        return True

    cache_key = None
    if anon_id:
        cache_key = f"turnstile_ok:{anon_id}"
    elif client_ip:
        cache_key = f"turnstile_ok_ip:{client_ip}"
    
    if cache_key and redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if cached:
                return True
        except Exception:
            pass

    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    data = {
        "secret": settings.CLOUDFLARE_TURNSTILE_SECRET,
        "response": token
    }
    
    logger.info(f"Initiating Turnstile verification check for: {cache_key}")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(url, data=data)
            response.raise_for_status()
            result = response.json()
            
            if result.get("success"):
                logger.info(f"Turnstile EXACT verification SUCCESS for: {cache_key}")
                if cache_key and redis_client:
                    try:
                        _ = await redis_client.setex(cache_key, 300, "1")
                        logger.info(f"Turnstile success cached (5m) for: {cache_key}")
                    except Exception as e:
                        logger.error(f"Failed to cache Turnstile success for {cache_key}: {e}")
                return True
            else:
                error_codes = result.get('error-codes', [])
                logger.warning(f"Turnstile verification FAILED for {cache_key}. Cloudflare Errors: {error_codes}")
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
        logger.error(f"Turnstile API returned error: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during Turnstile verification: {e}")
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
