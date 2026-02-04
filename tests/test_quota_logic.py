
import sys
import os
# import pytest
from unittest.mock import MagicMock
from datetime import datetime

# Adjust path
sys.path.append(os.getcwd())

# Mock modules
sys.modules["structlog"] = MagicMock()
sys.modules["sqlalchemy"] = MagicMock()
sys.modules["sqlalchemy.ext.asyncio"] = MagicMock()
sys.modules["asyncpg"] = MagicMock()
sys.modules["redis.asyncio"] = MagicMock()

from app.services.policy_engine import PolicyEngine, TierStatus

def test_quota_key_decision_logic():
    """
    Verifies the strict rule:
    If tier is PAID and entitlement is not stale -> quota key uses user_id
    Else -> quota key uses anon_id
    """
    
    user_id = "u_123"
    anon_id = "a_456"
    day = datetime.utcnow().strftime("%Y%m%d")
    
    # Case A: PAID + Fresh + user_id present
    # Expect: quota:user:{user_id}:{day}
    key_a = PolicyEngine.get_quota_key(
        user_id=user_id,
        anon_id=anon_id,
        tier=TierStatus.PAID,
        entitlement_stale=False
    )
    assert key_a == f"quota:user:{user_id}:{day}", f"Case A Failed: Got {key_a}"
    
    # Case B: PAID + Stale + user_id present
    # Expect: quota:anon:{anon_id}:{day}
    key_b = PolicyEngine.get_quota_key(
        user_id=user_id,
        anon_id=anon_id,
        tier=TierStatus.PAID,
        entitlement_stale=True
    )
    assert key_b == f"quota:anon:{anon_id}:{day}", f"Case B Failed: Got {key_b}"
    
    # Case C: FREE + user_id present
    # Expect: quota:anon:{anon_id}:{day}
    key_c = PolicyEngine.get_quota_key(
        user_id=user_id,
        anon_id=anon_id,
        tier=TierStatus.FREE,
        entitlement_stale=False # Freshness doesn't matter for FREE
    )
    assert key_c == f"quota:anon:{anon_id}:{day}", f"Case C Failed: Got {key_c}"
    
    # Case D: FREE + Stale + user_id present
    # Expect: quota:anon:{anon_id}:{day}
    key_d = PolicyEngine.get_quota_key(
        user_id=user_id,
        anon_id=anon_id,
        tier=TierStatus.FREE,
        entitlement_stale=True
    )
    assert key_d == f"quota:anon:{anon_id}:{day}", f"Case D Failed: Got {key_d}"
    
    # Case E: PAID + Fresh + No user_id (Should be impossible via middleware but robust check)
    # Expect: quota:anon:{anon_id}:{day}
    key_e = PolicyEngine.get_quota_key(
        user_id=None,
        anon_id=anon_id,
        tier=TierStatus.PAID,
        entitlement_stale=False
    )
    assert key_e == f"quota:anon:{anon_id}:{day}", f"Case E Failed: Got {key_e}"

    print("All Quota Key Logic Tests Passed!")

if __name__ == "__main__":
    test_quota_key_decision_logic()
