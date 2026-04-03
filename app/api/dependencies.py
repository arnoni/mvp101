
from fastapi import Request, HTTPException, status, Depends
from typing import Optional
from app.services.entitlement_service import TierStatus

async def get_current_user_id(request: Request) -> Optional[str]:
    """
    Returns the authenticated user ID from the session, or None.
    """
    return getattr(request.state, "user_id", None)

async def require_login(request: Request, user_id: Optional[str] = Depends(get_current_user_id)) -> str:
    """
    Enforces that a user is logged in.
    """
    if not user_id:
        # Check if session was attempted but failed (e.g. redis down)
        # We can infer this if dd_session cookie exists but user_id is None?
        # For now, simple 401.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required"
        )
    return user_id

async def require_paid(
    request: Request, 
    user_id: str = Depends(require_login)
) -> str:
    """
    Enforces that the user has a valid PAID subscription.
    Fails closed if entitlement cache is stale or missing.
    """
    tier = getattr(request.state, "tier", TierStatus.FREE)
    is_stale = getattr(request.state, "entitlement_stale", True)
    
    # 503: Entitlement verification failed due to transient issues (stale/miss)
    if is_stale:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "ENTITLEMENT_UNVERIFIED", "message": "Could not verify subscription status."}
        )
        
    # 403: User is logged in but not paid (and verification is fresh)
    if tier not in {TierStatus.PASS_1_DAY, TierStatus.PASS_3_DAY}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Paid subscription required."
        )
        
    return user_id
