# Implements TSD FR-002: Cloudflare Turnstile Verification
# Implements TSD Section 4.4: Business Logic (Verify Turnstile)
# Implements TSD Section 8: Validate and sanitize all inputs

from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
import httpx
import logging
from app.core.config import settings
from app.models.dto import ErrorResponse

logger = logging.getLogger(__name__)

async def verify_turnstile(token: str) -> bool:
    """
    Verifies the Cloudflare Turnstile token against the Cloudflare API.
    Implements TSD Section 6: Turnstile verification.
    """
    # Implements TSD Section 5.2: Turnstile secret stored in env var
    if settings.ENV == "development" and token == "mock_turnstile_token_for_testing":
        logger.warning("Using mock Turnstile token for development.")
        return True

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
