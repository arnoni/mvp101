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

class EntitlementMiddleware(BaseHTTPMiddleware):
    """
    Enforces server-side session presence for protected routes.
    Allowlist: /, /static, /health, /api/status, /api/pay
    """
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        allowlisted = (
            path == "/" or
            path.startswith("/static") or
            path.startswith("/health") or
            path.startswith("/api/status") or
            path.startswith("/api/pay")
        )
        if allowlisted:
            return await call_next(request)
        # Require Redis and session for protected routes
        redis_cli = getattr(request.app.state, "redis", None)
        if not redis_cli:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=503, content={"detail": "enforcement unavailable"})
        sid = request.cookies.get("dd_session")
        if not sid:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "session required"})
        session_key = f"session:{sid}"
        data = await redis_cli.get(session_key)
        if not data:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "session invalid"})
        try:
            import json
            payload = json.loads(data)
            request.state.session_id = sid
            request.state.tier = TierStatus(payload.get("tier", "FREE"))
            request.state.csrf = payload.get("csrf")
        except Exception:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=401, content={"detail": "session parse error"})
        return await call_next(request)
