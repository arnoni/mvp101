from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
import hashlib
from app.core.config import settings
from app.services.entitlement_service import TierStatus

class AnonIdMiddleware(BaseHTTPMiddleware):
    """
    Ensures every client has a 'dd_anon_id' for quota tracking.
    """
    async def dispatch(self, request: Request, call_next):
        # 1. Identity Resolution (Fingerprint-based with language component)
        anon_id = request.cookies.get("dd_anon_id")
        created_new = False
        lang_choice = request.cookies.get("dd_lang")
        
        # If no persisted language, derive a default from Accept-Language header
        if not lang_choice:
            accept_lang = request.headers.get("accept-language", "")
            # Simple best-effort extraction (e.g., "ru,en;q=0.9")
            lang_choice = (accept_lang.split(",")[0].split("-")[0] or "en").strip()
        
        if not anon_id:
            ua = request.headers.get("user-agent", "")
            accept_lang = request.headers.get("accept-language", "")
            fp_source = f"{ua}|{accept_lang}|{lang_choice}"
            anon_id = hashlib.sha256(fp_source.encode("utf-8")).hexdigest()
            created_new = True
            
        # Attach to request state for downstream usage (e.g. PolicyEngine)
        request.state.anon_id = anon_id
        
        # 2. Process Request
        response = await call_next(request)
        
        # 3. Set Cookies if new/derived
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
            response.set_cookie(
                key="dd_anon_id",
                value=anon_id,
                max_age=60 * 60 * 24 * 730,
                httponly=True,
                secure=(settings.ENV == "production"),
                samesite="strict"
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
