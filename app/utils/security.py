# Implements TSD FR-002: Cloudflare Turnstile Verification
# Implements TSD Section 4.4: Business Logic (Verify Turnstile)
# Implements TSD Section 8: Validate and sanitize all inputs

from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
import httpx
import logging
from app.core.config import settings
from app.models.dto import ErrorResponse
from typing import Optional
try:
    from app.services.redis_client import redis_client
except Exception:
    redis_client = None

logger = logging.getLogger(__name__)

async def protect_mutation(request: Request):
    # A. Enforce JSON only
    ct = request.headers.get("content-type", "")
    if "application/json" not in ct:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid content-type")
    # B. Enforce Origin/Referer
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    app_origin = settings.APP_ORIGIN or ""
    if not app_origin or not origin.startswith(app_origin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="origin not allowed")
    # C. Enforce CSRF token
    csrf_hdr = request.headers.get("x-csrf-token")
    csrf_state = getattr(request.state, "csrf", None)
    if not csrf_hdr or not csrf_state or csrf_hdr != csrf_state:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid csrf token")
    return True
async def verify_turnstile(token: str, anon_id: Optional[str] = None, client_ip: Optional[str] = None) -> bool:
    """
    Verifies the Cloudflare Turnstile token against the Cloudflare API.
    Implements TSD Section 6: Turnstile verification.
    """
    # Dev mode bypass removed as per user request.
    # if settings.ENV == "development" and token == "mock_turnstile_token_for_testing":
    #     logger.warning("Using mock Turnstile token for development.")
    #     return True
    
    cache_key = None
    if anon_id:
        cache_key = f"turnstile_ok:{anon_id}"
    elif client_ip:
        cache_key = f"turnstile_ok_ip:{client_ip}"
    
    if cache_key and redis_client:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                return True
        except Exception:
            pass

    url = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    data = {
        "secret": settings.CLOUDFLARE_TURNSTILE_SECRET,
        "response": token
    }
    
    # Implements TSD Section 6: 5s timeout
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(url, data=data)
            response.raise_for_status()
            result = response.json()
            
            if result.get("success"):
                if cache_key and redis_client:
                    try:
                        redis_client.setex(cache_key, 600, "1")
                    except Exception:
                        pass
                return True
            else:
                logger.warning(f"Turnstile verification failed: {result.get('error-codes')}")
                return False
    except httpx.TimeoutException:
        logger.error("Turnstile verification timed out.")
        # TSD Section 6: 5s timeout -> abort
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

def get_client_ip(request: Request) -> str:
    """
    Extracts the client's IP address from the request.
    Assumes a standard proxy setup (e.g., Vercel/Render) where the
    client IP is in the 'x-forwarded-for' header.
    """
    # Implements TSD FR-003: Rate Limiting (1 req/IP/24h)
    # This is a critical security/cost control point.
    
    # Check for common proxy headers
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        # The first IP is the client's IP
        return x_forwarded_for.split(',')[0].strip()
    
    # Fallback to direct client host
    return request.client.host if request.client else "unknown_ip"
