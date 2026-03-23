import sentry_sdk  # <--- NEW IMPORT
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
import hashlib
import hmac
import uuid
import json
from typing import Optional
from app.core.config import settings
from app.services.entitlement_service import EntitlementService
from app.services.redis_client import redis_client

# --- Helper Functions (No changes needed) ---
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
        
        # --- SENTRY INTEGRATION START ---
        # We set this immediately. If they log in later (SessionMiddleware), 
        # the user info will be updated/merged.
        sentry_sdk.set_tag("anon_id", anon_id)
        sentry_sdk.set_user({"id": anon_id, "ip_address": "{{auto}}"})
        # --- SENTRY INTEGRATION END ---

        # Language resolution
        lang_choice = request.cookies.get("dd_lang")
        if not lang_choice:
            accept_lang = request.headers.get("accept-language", "")
            # Simple safe split
            lang_choice = (accept_lang.split(",")[0].split("-")[0] or "en").strip()
            
        # 2. Process Request
        response = await call_next(request)
        
        # 3. Set Cookies
        # (Logic preserved)
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
    async def dispatch(self, request: Request, call_next):
        sid = request.cookies.get("dd_session")
        
        request.state.session_id = None
        request.state.user_id = None
        request.state.csrf = None
        
        if redis_client and sid:
            try:
                session_key = f"session:{sid}"
                data = await redis_client.get(session_key)
                if data:
                    payload = json.loads(data)
                    user_id = payload.get("user_id")
                    
                    request.state.session_id = sid
                    request.state.user_id = user_id
                    request.state.csrf = payload.get("csrf")

                    # --- SENTRY INTEGRATION START ---
                    # We found a real user. Overwrite the anonymous Sentry User.
                    # This links the previous Anon actions to this real User ID.
                    sentry_sdk.set_user({
                        "id": str(user_id),
                        "username": payload.get("username", "unknown"), # Optional if available
                        "session_id": sid
                    })
                    # --- SENTRY INTEGRATION END ---

            except Exception as e:
                # --- IMPROVEMENT: DON'T SWALLOW SILENTLY ---
                # If Redis fails, we want to know, but we don't want to crash the user's request.
                # 'capture_exception' sends it to Sentry but lets code continue.
                sentry_sdk.capture_exception(e)
                # We proceed as if logged out
                pass
                
        return await call_next(request)

class EntitlementMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        identity_id = getattr(request.state, "identity_id", None)

        # Use the Redis client from app state if available
        redis_cli = getattr(request.app.state, "redis", None)

        result = await EntitlementService.get_tier(identity_id, redis_cli)
        request.state.tier = result.tier

        return await call_next(request)