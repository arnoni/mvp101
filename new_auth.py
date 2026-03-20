from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import RedirectResponse
import logging

# Note: Import your actual services/repositories here
# from app.services.token_service import magic_token_service
# from app.services.db_service import db
# from app.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])

@router.get("/magic")
async def consume_magic_link(token: str, request: Request, response: Response):
    """
    Consumes the magic link clicked from an email.
    """
    try:
        # 1. Verify token signature and expiration (e.g., 15 mins)
        payload = magic_token_service.verify(token, max_age_seconds=900)
        email = payload.get("email")
        purchase_id = payload.get("purchase_id")
        
        # 2. Check token idempotency (has it been used?)
        if await magic_token_service.is_consumed(token):
            return RedirectResponse(url="/?error=link_used", status_code=303)
            
        # 3. Verify purchase exists and is paid in the database
        purchase = await db.get_purchase(purchase_id)
        if not purchase or purchase.status != "paid":
            return RedirectResponse(url="/?error=unpaid", status_code=303)
            
        # 4. Activate Pass and consume token atomically
        pass_expires_at = await db.activate_pass(email=email, plan=purchase.plan)
        await magic_token_service.consume(token)

        # 5. Create durable session identifier
        session_token = session_manager.create_session(email=email, expires_at=pass_expires_at)

    except Exception as e:
        logging.error(f"Magic link consumption failed: {e}")
        return RedirectResponse(url="/?error=invalid_link", status_code=303)

    # 6. Redirect to UI with success state and secure HttpOnly cookie
    # This prevents the raw token from lingering in the browser history/URL
    redirect_response = RedirectResponse(url="/?magic_success=1", status_code=303)
    redirect_response.set_cookie(
        key="dd_session",
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=True, # Requires HTTPS in production
        max_age=86400 * 3, # 3 days max age (adjust based on plan)
        path="/"
    )
    
    return redirect_response