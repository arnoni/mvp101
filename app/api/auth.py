import secrets
import time
import json
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr
from app.services.magic_link_service import MagicLinkService
from app.core.config import settings
from app.core.keys import KeyBuilder

router = APIRouter()

class LoginRequest(BaseModel):
    email: EmailStr

@router.post("/login")
async def login(payload: LoginRequest, request: Request):
    engine = getattr(request.app.state, "db_engine", None)
    if not engine:
        raise HTTPException(503, "Database unavailable")
    
    service = MagicLinkService(engine)
    token = await service.create_magic_link(payload.email)
    
    # Mock Email Sending
    # In production, use Resend or SES here
    magic_link = f"{settings.APP_ORIGIN or 'http://localhost:8000'}/api/auth/magic?token={token}"
    
    if settings.ENV == "development":
        print(f"MAGIC LINK for {payload.email}: {magic_link}")
    
    return {"message": "Magic link sent"}

@router.get("/magic")
async def magic_landing(token: str, request: Request, response: Response):
    """
    Exchanges magic token for session cookie.
    """
    engine = getattr(request.app.state, "db_engine", None)
    redis_cli = getattr(request.app.state, "redis", None)
    
    if not engine or not redis_cli:
        raise HTTPException(503, "Service unavailable")
        
    service = MagicLinkService(engine)
    user_id = await service.redeem_token(token)
    
    if not user_id:
        raise HTTPException(401, "Invalid or expired token")
        
    # Create Session
    sid = secrets.token_urlsafe(32)
    session_data = {
        "user_id": user_id,
        "created_at": int(time.time()),
        "csrf": secrets.token_hex(16)
    }
    
    session_key = KeyBuilder.session(sid)
    await redis_cli.set(session_key, json.dumps(session_data), ex=settings.SESSION_TTL_SECONDS)
    
    response.set_cookie(
        key=settings.SESSION_COOKIE_NAME,
        value=sid,
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=(settings.ENV == "production"),
        samesite="lax"
    )
    
    return {"message": "Logged in", "user_id": user_id}

@router.post("/logout")
async def logout(request: Request, response: Response):
    sid = request.cookies.get(settings.SESSION_COOKIE_NAME)
    redis_cli = getattr(request.app.state, "redis", None)
    
    if sid and redis_cli:
        await redis_cli.delete(KeyBuilder.session(sid))
        
    response.delete_cookie(settings.SESSION_COOKIE_NAME)
    return {"message": "Logged out"}
