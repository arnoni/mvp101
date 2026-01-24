from enum import Enum
from typing import Optional, Protocol
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
        # Key strategy: "quota:{date}:{anon_id}" or "quota:{date}:{user_id}"
        # For MVP we can rely on anon_id or ip if anon_id is missing (though middleware should ensure it)
        # We'll assume the context always has a valid identifier.
        # Note: The caller is responsible for INCREMENTING the quota if allowed.
        # Here we just peek. Or we can have `check_and_incr` logic.
        # TDD suggests "Check Quota".
        
        # Scope quota per day to avoid permanent accumulation
        from datetime import datetime
        day = datetime.utcnow().strftime("%Y%m%d")
        quota_key = f"daily_read:{day}:{context.anon_id}"
        
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
