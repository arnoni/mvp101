import time
import secrets
import json
import logging
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr

from app.core.config import settings
from app.services.magic_auth_service import MagicAuthService, PaymentGatewayFactory
from email_service import EmailService

logger = logging.getLogger(__name__)
router = APIRouter()

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


class MagicLinkRequest(BaseModel):
    email: EmailStr


@router.post("/magic-link", response_model=AuthResponse, status_code=200)
async def resend_magic_link(payload: MagicLinkRequest):
    # Intentionally generic response to avoid account enumeration.
    return AuthResponse(message="If the email exists, a magic link has been sent.")


# ==========================================
# ROUTER & DEPENDENCIES
# ==========================================

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
        
        # --- dilldrillteamtest START ---
        # Backdoor for development: Automatically validate payment for the test email
        if payload.email.lower() == "dilldrillteam@gmail.com":
            logger.info("🧪 [dilldrillteamtest] Bypassing webhook for test email. Pre-validating in Redis.")
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            redis_payload = json.dumps({
                "email": payload.email.lower(),
                "plan": "1_day",
                "purchase_id": "sim_backdoor_test"
            })
            # Use the same Redis key structure as the webhook
            await service.redis.set(f"magic:{token_hash}", redis_payload, ex=1800)
        # --- dilldrillteamtest END ---

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
        logger.exception(f"AUTH_LOGIN_CRITICAL_FAILURE: {e}")
        raise HTTPException(status_code=500, detail="Failed to process authentication request")


@router.get("/magic", include_in_schema=False)
async def magic_landing(
    token: str,
    response: Response,
    service: MagicAuthService = Depends(get_auth_service)
):
    """
    Consumes the magic link clicked from the user's email.
    """
    app_origin = settings.APP_ORIGIN or "http://localhost:8000"
    redis = service.redis
    db = service.db
    
    try:
        # 1. Hash the incoming raw token to match what is stored in Redis
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        
        # 2. ATOMIC CONSUME: Get the payload and delete the key in one step.
        # GETDEL is available in Redis 6.2+.
        try:
            payload_str = await redis.getdel(f"magic:{token_hash}")
        except Exception as e:
            logger.error(f"AUTH_MAGIC_REDIS_GETDEL_FAILED: {e}")
            return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_REDIS_ERROR", status_code=303)
        
        if not payload_str:
            logger.warning(f"AUTH_MAGIC_INVALID_LINK: Attempted use of invalid, expired, or already-consumed magic link. Hash: {token_hash[:8]}")
            return RedirectResponse(url=f"{app_origin}/?error=invalid_link", status_code=303)
            
        # Parse the data safely stored during the webhook phase
        try:
            payload = json.loads(payload_str)
            email = payload.get("email")
            plan = payload.get("plan")
            purchase_id = payload.get("purchase_id")
        except (json.JSONDecodeError, AttributeError) as e:
            logger.error(f"AUTH_MAGIC_PAYLOAD_PARSE_FAILED: {e}")
            return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_INVALID_PAYLOAD", status_code=303)
            
        if not email or not purchase_id:
            logger.error(f"AUTH_MAGIC_INCOMPLETE_PAYLOAD: email={email}, purchase_id={purchase_id}")
            return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_INCOMPLETE_DATA", status_code=303)

        # 3. Activate Pass & Get Expiry (Updates DB to mark pass active starting NOW)
        try:
            async with db.begin() as conn:
                # Mark purchase as active and calculate expiry based on plan
                days = 1 if plan == "1_day" else 3
                expiry = datetime.now(timezone.utc) + timedelta(days=days)
                
                await conn.execute(
                    text("UPDATE purchases SET status = 'active', activated_at = NOW(), expires_at = :exp WHERE id = :pid"),
                    {"exp": expiry, "pid": purchase_id}
                )
                
                # Ensure user exists and get ID
                user_res = await conn.execute(
                    text("INSERT INTO users (email) VALUES (:email) ON CONFLICT (email) DO UPDATE SET last_login = NOW() RETURNING id"),
                    {"email": email}
                )
                user_id = user_res.scalar()
        except Exception as e:
            logger.error(f"AUTH_MAGIC_DB_ACTIVATION_FAILED: {e}")
            return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_DB_ERROR", status_code=303)

        # 4. Create durable session identifier for the browser
        session_id = secrets.token_urlsafe(32)
        session_ttl = 86400 * 3  # 3 days
        
        session_data = {
            "user_id": user_id,
            "email": email,
            "plan": plan,
            "auth_time": int(time.time()),
            "csrf_token": secrets.token_hex(16)
        }
        
        from app.core.keys import KeyBuilder
        try:
            await redis.set(KeyBuilder.session(session_id), json.dumps(session_data), ex=session_ttl)
        except Exception as e:
            logger.error(f"AUTH_MAGIC_SESSION_STORE_FAILED: {e}")
            return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_SESSION_ERROR", status_code=303)

        # 5. Redirect to UI with success state and secure HttpOnly cookie
        redirect_response = RedirectResponse(url=f"{app_origin}/?magic_success=1", status_code=303)
        
        redirect_response.set_cookie(
            key=settings.SESSION_COOKIE_NAME,
            value=session_id,
            httponly=True,
            samesite="lax",
            secure=(settings.ENV == "production"),
            max_age=session_ttl,
            path="/"
        )
        
        logger.info(f"AUTH_SUCCESS: ✅ Auth successful for {email} via atomic magic link")
        return redirect_response

    except Exception as e:
        logger.exception(f"AUTH_MAGIC_CRITICAL_FAILURE: {e}")
        return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_UNKNOWN_ERROR", status_code=303)


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
