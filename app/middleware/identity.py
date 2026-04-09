import json
import uuid
import logging
import sentry_sdk
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from app.core.config import settings
from app.core.keys import KeyBuilder

logger = logging.getLogger(__name__)

class IdentityMiddleware(BaseHTTPMiddleware):
    """
    Guarantees that every request has a deterministic identity:
    - request.state.identity_kind: "paid" | "anon"
    - request.state.identity_id: UUID string (user_id OR anon_id)
    - request.state.session_id: str (if paid)
    
    Order of precedence:
    1. Valid Session Cookie (Paid)
    2. Valid Anon Cookie (Anon)
    3. New Anon Identity
    """
    
    async def dispatch(self, request: Request, call_next):
        from app.services.redis_client import redis_client
        
        # 1. Try to resolve Session (Paid)
        session_cookie = request.cookies.get(settings.SESSION_COOKIE_NAME)
        if session_cookie and redis_client:
            session_key = KeyBuilder.session(session_cookie)
            raw_session = await redis_client.get(session_key)
            if raw_session:
                try:
                    data = json.loads(raw_session)
                    user_id = data.get("user_id")
                    if user_id:
                        # SUCCESS: Paid Identity
                        request.state.identity_kind = "paid"
                        request.state.identity_id = user_id
                        request.state.session_id = session_cookie
                        
                        # Backward Compatibility
                        request.state.user_id = user_id
                        request.state.anon_id = None 
                        
                        sentry_sdk.set_user({"id": user_id})
                        sentry_sdk.set_tag("identity_kind", "paid")
                        
                        return await call_next(request)

                except Exception as e:
                    logger.error(f"IDENTITY_SESSION_CORRUPT: {e}", session_id=session_cookie)
                    pass # Corrupt session, fall through

        # 2. Try to resolve Anon (Free)
        anon_cookie = request.cookies.get(settings.ANON_COOKIE_NAME)
        anon_id = anon_cookie
        
        # Validate UUID format
        if anon_id:
            try:
                uuid.UUID(anon_id)
            except ValueError:
                anon_id = None
        
        new_anon = False
        if not anon_id:
            anon_id = str(uuid.uuid4())
            new_anon = True
            
        # SUCCESS: Anon Identity
        request.state.identity_kind = "anon"
        request.state.identity_id = anon_id
        request.state.session_id = None
        
        # Backward Compatibility
        request.state.anon_id = anon_id
        request.state.user_id = None
        
        sentry_sdk.set_user({"id": anon_id, "ip_address": "{{auto}}"})
        sentry_sdk.set_tag("identity_kind", "anon")

        response = await call_next(request)
        
        # 3. Set Anon Cookie if needed
        # We set it for a very long time (2 years)
        if new_anon:
            response.set_cookie(
                key=settings.ANON_COOKIE_NAME,
                value=anon_id,
                max_age=63072000, # 2 years
                httponly=True,
                secure=(settings.ENV == "production"),
                samesite="lax"
            )
            
        return response
