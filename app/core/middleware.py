from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
import hashlib
import hmac
import uuid
from typing import Optional
from app.core.config import settings
from app.services.entitlement_service import TierStatus

def sign_value(val: str) -> str:
    sig = hmac.new(settings.SECRET_KEY.encode(), val.encode(), hashlib.sha256).hexdigest()
    return f"{val}.{sig}"

def unsign_value(val: str) -> Optional[str]:
    try:
        payload, sig = val.rsplit(".", 1)
        expected_sig = hmac.new(settings.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected_sig):
            return payload
    except Exception:
        pass
    return None

class AnonIdMiddleware(BaseHTTPMiddleware):
    """
    Ensures every client has a signed 'dd_anon_id' for quota tracking.
    Minted randomly (UUID) if missing or invalid signature.
    """
    async def dispatch(self, request: Request, call_next):
        # 1. Identity Resolution
        raw_cookie = request.cookies.get("dd_anon_id")
        anon_id = None
        
        if raw_cookie:
            anon_id = unsign_value(raw_cookie)
            
        created_new = False
        if not anon_id:
            anon_id = uuid.uuid4().hex
            created_new = True

        # Attach to request state
        request.state.anon_id = anon_id
        
        # Language resolution (preserve existing logic)
        lang_choice = request.cookies.get("dd_lang")
        if not lang_choice:
            accept_lang = request.headers.get("accept-language", "")
            lang_choice = (accept_lang.split(",")[0].split("-")[0] or "en").strip()
            
        # 2. Process Request
        response = await call_next(request)
        
        # 3. Set Cookies
        if not request.cookies.get("dd_lang"):
            response.set_cookie(
                key="dd_lang",
                value=lang_choice or "en",
                max_age=60 * 60 * 24 * 180,
                httponly=False,
                secure=(settings.ENV == "production"),
                samesite="lax"
            )
            
        if created_new:
            signed_val = sign_value(anon_id)
            response.set_cookie(
                key="dd_anon_id",
                value=signed_val,
                max_age=60 * 60 * 24 * 730,
                httponly=True,
                secure=(settings.ENV == "production"),
                samesite="lax"
            )
            
        return response

class SessionMiddleware(BaseHTTPMiddleware):
    """
    Hydrates user session from Redis if 'dd_session' cookie is present.
    Sets request.state.session_id, request.state.user_id, request.state.csrf.
    Does NOT determine tier.
    """
    async def dispatch(self, request: Request, call_next):
        redis_cli = getattr(request.app.state, "redis", None)
        sid = request.cookies.get("dd_session")
        
        # Initialize state
        request.state.session_id = None
        request.state.user_id = None
        request.state.csrf = None
        
        if redis_cli and sid:
            try:
                session_key = f"session:{sid}"
                data = await redis_cli.get(session_key)
                if data:
                    import json
                    payload = json.loads(data)
                    request.state.session_id = sid
                    request.state.user_id = payload.get("user_id")
                    request.state.csrf = payload.get("csrf")
            except Exception:
                pass
                
        return await call_next(request)

class EntitlementMiddleware(BaseHTTPMiddleware):
    """
    Determines user tier using EntitlementService.
    Enforces authentication for protected routes.
    Allowlist: /, /static, /health, /api/status
    """
    async def dispatch(self, request: Request, call_next):
        from app.services.entitlement_service import EntitlementService
        
        path = request.url.path
        allowlisted = (
            path == "/" or
            path.startswith("/static") or
            path.startswith("/health") or
            path.startswith("/api/status")
        )
        
        # 1. Determine Tier (independent of route protection)
        user_id = getattr(request.state, "user_id", None)
        redis_cli = getattr(request.app.state, "redis", None)
        
        # Check entitlement cache
        # We assume 5 minutes TTL for freshness check
        entitlement_result = await EntitlementService.get_tier(user_id, redis_cli, ttl_seconds=300)
        
        request.state.tier = entitlement_result.tier
        request.state.entitlement_stale = entitlement_result.is_stale
        request.state.entitlement_data = entitlement_result.raw_data
        
        # 2. Enforce Protection (Explicit dependencies only - no path prefix blocking)
        # Note: Previous logic for path-based blocking is removed as per policy update.
        # Protection is now handled by Depends(require_paid) or Depends(require_login).
            
        return await call_next(request)
