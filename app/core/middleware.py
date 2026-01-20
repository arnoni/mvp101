import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

class AnonIdMiddleware(BaseHTTPMiddleware):
    """
    Ensures every client has a 'dd_anon_id' for quota tracking.
    """
    async def dispatch(self, request: Request, call_next):
        # 1. Identity Resolution
        anon_id = request.cookies.get("dd_anon_id")
        created_new = False
        
        if not anon_id:
            anon_id = str(uuid.uuid4())
            created_new = True
            
        # Attach to request state for downstream usage (e.g. PolicyEngine)
        request.state.anon_id = anon_id
        
        # 2. Process Request
        response = await call_next(request)
        
        # 3. Set Cookie if new
        if created_new:
            # 90 days expiration
            response.set_cookie(
                key="dd_anon_id",
                value=anon_id,
                max_age=60 * 60 * 24 * 90,
                httponly=True,
                secure=False, # Set to True in Prod usually, but TDD v1 implies localhost dev mostly for now
                samesite="lax"
            )
            
        return response
