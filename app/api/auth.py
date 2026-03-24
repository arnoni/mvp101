import time
import secrets
import json
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

from app.core.config import settings
from app.services.magic_auth_service import MagicAuthService, PaymentGatewayFactory
from email_service import EmailService

logger = logging.getLogger(__name__)

# ==========================================
# SCHEMAS
# ==========================================

class LoginRequest(BaseModel):
    email: EmailStr
    purchase_id: Optional[str] = None
    provider: Optional[str] = None  # "paddle", "lemonsqueezy", or "dodo"

class AuthResponse(BaseModel):
    message: str

class LogoutResponse(BaseModel):
    message: str


# ==========================================
# ROUTER & DEPENDENCIES
# ==========================================

router = APIRouter()

def get_auth_service(request: Request) -> MagicAuthService:
    """Dependency injector for the auth service."""
    engine = getattr(request.app.state, "db_engine", None)
    redis_cli = getattr(request.app.state, "redis", None)
    
    if not engine or not redis_cli:
        raise HTTPException(status_code=503, detail="Database or cache service unavailable")
    
    return MagicAuthService(
        db=engine,
        redis=redis_cli,
        payment_factory=PaymentGatewayFactory()
    )


# ==========================================
# ENDPOINTS
# ==========================================

@router.post("/login", response_model=AuthResponse, status_code=200)
async def login(
    payload: LoginRequest,
    request: Request,
    service: MagicAuthService = Depends(get_auth_service)
):
    """
    Initiates magic link flow: creates a token (optionally tied to a MoR purchase) and "sends" it.
    """
    try:
        # Pass the purchase_id & provider to bind the payment strictly to this auth token
        token = await service.create_magic_link(
            email=payload.email,
            purchase_id=payload.purchase_id,
            provider=payload.provider
        )
        
        # Build the callback URL
        app_origin = settings.APP_ORIGIN or "http://localhost:8000"
        magic_link = f"{app_origin}/api/auth/magic?token={token}"
        
        # Send via Resend
        email_service = EmailService()
        sent = await email_service.send_magic_link(
            email=payload.email, 
            magic_link=magic_link,
            expire_minutes=settings.MAGICLINK_EXPIRY_MINUTES
        )
        
        if not sent:
            # Fallback for dev environment if Resend is not configured
            if settings.ENV == "development":
                logger.info(f"🔐 [DEV FALLBACK] MAGIC LINK for {payload.email}: {magic_link}")
                return AuthResponse(message="Magic link logged to console (Dev Mode).")
            raise HTTPException(status_code=500, detail="Failed to send magic link email")
        
        return AuthResponse(message="Magic link sent. Check your inbox.")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login request failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to process authentication request")


@router.get("/magic", include_in_schema=False)
async def magic_landing(
    token: str,
    response: Response,
    service: MagicAuthService = Depends(get_auth_service)
):
    """
    Consumes magic link: 
    1. Validates token cryptographically.
    2. Enforces one-time use atomically in Postgres.
    3. Queries Paddle/Dodo/LemonSqueezy APIs if webhook hasn't fired yet.
    4. Sets an ultra-fast Redis session and strict secure cookie.
    """
    app_origin = settings.APP_ORIGIN or "http://localhost:8000"
    
    try:
        auth_result = await service.redeem_token(token)
        
        if not auth_result.success:
            logger.warning(f"Auth failed for token redemption: {auth_result.error}")
            return RedirectResponse(
                url=f"{app_origin}/?error={auth_result.error_code or 'invalid_link'}",
                status_code=303
            )
            
        # Success! Create ultra-fast Redis session
        session_id = secrets.token_urlsafe(32)
        session_ttl = settings.SESSION_TTL_SECONDS
        
        session_data = {
            "user_id": auth_result.user_id,
            "email": auth_result.email,
            "auth_time": int(time.time()),
            "csrf_token": secrets.token_hex(16)
        }
        
        # Store in Redis
        from app.core.keys import KeyBuilder
        session_key = KeyBuilder.session(session_id)
        await service.redis.set(
            session_key, 
            json.dumps(session_data),
            ex=session_ttl
        )
        
        # Set Strict Secure Cookie
        response.set_cookie(
            key=settings.SESSION_COOKIE_NAME,
            value=session_id,
            max_age=session_ttl,
            httponly=True,
            secure=(settings.ENV == "production"),
            samesite="lax",
            path="/"
        )
        
        logger.info(f"✅ Auth successful for {auth_result.email}")
        return RedirectResponse(url=f"{app_origin}/?magic_success=1", status_code=302)
        
    except Exception as e:
        logger.error(f"Critical error during auth magic consumption: {e}", exc_info=True)
        return RedirectResponse(url=f"{app_origin}/?error=server_error", status_code=303)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    service: MagicAuthService = Depends(get_auth_service)
):
    """
    Invalidates session: safely deletes the Redis record and clears the client cookie.
    """
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    
    if session_id:
        from app.core.keys import KeyBuilder
        await service.redis.delete(KeyBuilder.session(session_id))
        logger.debug(f"🗑️ Session invalidated in Redis: {session_id[:8]}...")
    
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
        secure=(settings.ENV == "production"),
        httponly=True,
        samesite="lax"
    )
    
    return LogoutResponse(message="Logged out successfully")
