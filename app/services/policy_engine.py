from enum import Enum
from typing import Optional, Protocol
from dataclasses import dataclass
from fastapi import Request, HTTPException, status
from app.core.config import settings
from app.utils.security import verify_turnstile, get_client_ip
from app.models.dto import ErrorResponse
from app.services.quota_repository import QuotaRepository
from pydantic import BaseModel
from app.services.entitlement_service import TierStatus

# --- Contracts ---

class PolicyVerdict(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"

class FrictionType(str, Enum):
    TURNSTILE = "TURNSTILE"

class RequestContext(BaseModel):
    anon_id: str
    paid_tier: TierStatus
    area_code: str
    client_ip: str
    turnstile_token: Optional[str] = None
    user_id: Optional[str] = None
    entitlement_stale: bool = False

class PolicyDecision(BaseModel):
    verdict: PolicyVerdict
    quota_remaining: int
    max_results: int
    friction_type: Optional[FrictionType] = None
    retry_after: Optional[int] = None # Seconds

class QuotaInterface(Protocol):
    """Abstract interface for checking usage."""
    async def get_usage(self, key: str) -> int: ...
    async def check_available(self, key: str, max_limit: int) -> bool: ...


# --- Policy Engine ---

class PolicyEngine:
    """
    Centralize all 'can I proceed?' logic.
    """
    
    # Policy Constants
    FREE_TIER_DAILY_LIMIT = 2
    PAID_TIER_DAILY_LIMIT = 50
    
    FREE_TIER_RESULTS = 1
    PAID_TIER_RESULTS = 5
    
    def __init__(self, quota_repo: QuotaInterface):
        self.quota_repo = quota_repo

    @staticmethod
    def get_quota_key(user_id: Optional[str], anon_id: str, tier: TierStatus, entitlement_stale: bool) -> str:
        from datetime import datetime
        day = datetime.utcnow().strftime("%Y%m%d")
        
        # STRICT RULE: If tier is PAID and entitlement is not stale -> user_id
        if tier == TierStatus.PAID and not entitlement_stale and user_id:
             return f"quota:user:{user_id}:{day}"
        
        # Fallback to anon_id
        return f"quota:anon:{anon_id}:{day}"

    async def evaluate(self, context: RequestContext) -> PolicyDecision:
        """
        Evaluates the request context against policy rules.
        """
        
        # 1. Determine Limits based on Tier
        limit = self.FREE_TIER_DAILY_LIMIT
        max_results = self.FREE_TIER_RESULTS
        
        if context.paid_tier == TierStatus.PAID:
            limit = self.PAID_TIER_DAILY_LIMIT
            max_results = self.PAID_TIER_RESULTS
            
        # 2. Check Quota
        quota_key = self.get_quota_key(
            context.user_id, 
            context.anon_id, 
            context.paid_tier, 
            context.entitlement_stale
        )
        
        current_usage = await self.quota_repo.get_usage(quota_key)
        quota_remaining = max(0, limit - current_usage)
        
        if current_usage >= limit:
            return PolicyDecision(
                verdict=PolicyVerdict.BLOCK,
                quota_remaining=0,
                max_results=max_results,
                retry_after=3600 * 24 # Try again tomorrow (simplified)
            )
            
        # 3. Friction / Turnstile Check
        # If policy demands Turnstile provided, check if it's there?
        # The TDD says: "Do not perform Turnstile validation here; only signal if it was required but missing."
        # For now, we can say Turnstile is required for all Free Tier requests to prevent scraping?
        # Or maybe only if usage > X?
        # Let's enforce Turnstile for ALL requests in this MVP phase as per TDD "Turnstile ... as escalation".
        # Actually TDD says "PoW as first friction... Turnstile as escalation".
        # But Phase 6.2 says "Decision: PoW is deferred. Implementation: PolicyEngine returns friction_type=TURNSTILE only."
        # So we always request Turnstile if not present, or maybe just return it as a friction type.
        
        # New Rule: If context.turnstile_token is missing, we require it.
        # But wait, the client should send it. If valid, the Route verifies it.
        # The PolicyEngine just says "I need 'CHALLENGE_REQUIRED' if I don't see a valid signal".
        # But "valid signal" verification happens in Route?
        # "Logic: ... Return Decision. Do not perform Turnstile validation here; only signal if it was required but missing."
        
        # So: if context.turnstile_token provided?
        # The context just has the token string. We don't verify it here.
        # If the ROUTE has already verified it, maybe it passes a flag `turnstile_verified=True`?
        # The TDD input contract says `turnstile_token (optional str)`.
        
        # Let's assume if it's MISSING, we return CHALLENGE_REQUIRED.
        if not context.turnstile_token and context.paid_tier == TierStatus.FREE:
             return PolicyDecision(
                verdict=PolicyVerdict.CHALLENGE_REQUIRED,
                quota_remaining=quota_remaining,
                max_results=max_results,
                friction_type=FrictionType.TURNSTILE
            )
            
        # If we are here, we are good to go (assuming token is valid, which Route checks).
        return PolicyDecision(
            verdict=PolicyVerdict.ALLOW,
            quota_remaining=quota_remaining,
            max_results=max_results
        )

@dataclass
class GateResult:
    decision: PolicyDecision
    remaining_after: int
    admin_bypass: bool

async def run_gate(
    request: Request,
    data_turnstile_token: Optional[str],
    policy_engine: "PolicyEngine",
    quota_repo: QuotaRepository,
    anon_id: str,
    user_id: Optional[str],
    tier: TierStatus,
    entitlement_stale: bool,
    area_code: str,
    force_turnstile_required: bool = False,
    disallow_admin_bypass: bool = False,
) -> GateResult:
    admin_hdr = request.headers.get("X-Admin-Auth")
    admin_bypass = bool(settings.ADMIN_BYPASS_TOKEN and admin_hdr and admin_hdr == settings.ADMIN_BYPASS_TOKEN)
    if disallow_admin_bypass:
        admin_bypass = False

    client_ip = get_client_ip(request)
    context = RequestContext(
        anon_id=anon_id,
        paid_tier=tier,
        area_code=area_code,
        client_ip=client_ip,
        turnstile_token=data_turnstile_token,
        user_id=user_id,
        entitlement_stale=entitlement_stale,
    )

    if admin_bypass:
        decision = PolicyDecision(verdict=PolicyVerdict.ALLOW, quota_remaining=999, max_results=5)
        return GateResult(decision=decision, remaining_after=decision.quota_remaining or 999, admin_bypass=True)

    decision = await policy_engine.evaluate(context)

    if decision.verdict == PolicyVerdict.BLOCK:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ErrorResponse(
                error="QUOTA_EXCEEDED",
                detail="Daily quota exceeded.",
                retry_after_seconds=decision.retry_after,
                quota_remaining=decision.quota_remaining,
                error_id=getattr(request.state, "request_id", None),
            ).model_dump(),
        )

    if decision.verdict == PolicyVerdict.CHALLENGE_REQUIRED or force_turnstile_required:
        if not data_turnstile_token:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ErrorResponse(
                    error="CHALLENGE_REQUIRED",
                    detail="Human verification required.",
                    quota_remaining=decision.quota_remaining,
                    error_id=getattr(request.state, "request_id", None),
                ).model_dump(),
            )
        ok = await verify_turnstile(token=data_turnstile_token, anon_id=anon_id, client_ip=client_ip)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ErrorResponse(
                    error="INVALID_CHALLENGE",
                    detail="Verification failed. Please try again.",
                    quota_remaining=decision.quota_remaining,
                    error_id=getattr(request.state, "request_id", None),
                ).model_dump(),
            )

    quota_key = PolicyEngine.get_quota_key(user_id, anon_id, tier, entitlement_stale)

    limit = PolicyEngine.FREE_TIER_DAILY_LIMIT if tier == TierStatus.FREE else PolicyEngine.PAID_TIER_DAILY_LIMIT

    try:
        allowed, remaining_after = await quota_repo.check_and_consume(quota_key, limit)
    except RuntimeError:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ErrorResponse(
                error="QUOTA_EXCEEDED",
                detail="Daily quota exceeded.",
                retry_after_seconds=3600 * 24,
                quota_remaining=0,
                error_id=getattr(request.state, "request_id", None),
            ).model_dump(),
        )

    return GateResult(decision=decision, remaining_after=remaining_after, admin_bypass=False)
