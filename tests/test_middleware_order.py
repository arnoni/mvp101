
import sys
import os
# import pytest
from fastapi import FastAPI, Request, Depends
from starlette.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock
import json
import time

# Adjust path to include app
sys.path.append(os.getcwd())

# Mock modules before imports
sys.modules["structlog"] = MagicMock()
sys.modules["sqlalchemy"] = MagicMock()
sys.modules["sqlalchemy.ext.asyncio"] = MagicMock()
sys.modules["asyncpg"] = MagicMock()

from app.core.middleware import SessionMiddleware, EntitlementMiddleware
from app.services.entitlement_service import EntitlementService, TierStatus, EntitlementResult
from app.api.dependencies import require_paid

def test_middleware_order_and_entitlement():
    app = FastAPI()
    
    # Mock Redis
    redis_mock = AsyncMock()
    app.state.redis = redis_mock
    
    # Track execution order
    execution_order = []
    
    @app.middleware("http")
    async def tracker_middleware(request: Request, call_next):
        # Check state before EntitlementMiddleware (but after SessionMiddleware)
        # This is tricky because we can't insert middleware in the middle easily here 
        # without manually constructing the stack.
        # Instead, we'll check the final state in the endpoint.
        response = await call_next(request)
        return response

    # Add middlewares
    # Order: Session (Outer) -> Entitlement (Inner) -> App
    app.add_middleware(EntitlementMiddleware)
    app.add_middleware(SessionMiddleware)
    
    @app.get("/debug")
    async def debug_endpoint(request: Request):
        return {
            "user_id": getattr(request.state, "user_id", None),
            "tier": getattr(request.state, "tier", None),
            "entitlement_stale": getattr(request.state, "entitlement_stale", None)
        }
        
    @app.get("/paid-only", dependencies=[Depends(require_paid)])
    async def paid_endpoint():
        return {"status": "success"}

    client = TestClient(app)
    
    # 1. Test Session Hydration & Entitlement Flow
    sid = "test_session_123"
    user_id = "user_456"
    
    # Setup Redis for Session
    async def mock_redis_get(key):
        if key == f"session:{sid}":
            return json.dumps({"user_id": user_id, "csrf": "abc"})
        if key == f"entitlement:user:{user_id}":
            return json.dumps({
                "tier": "PAID",
                "verified_at": int(time.time()) # Fresh
            })
        return None
        
    redis_mock.get.side_effect = mock_redis_get
    
    # Make request
    response = client.get("/debug", cookies={"dd_session": sid})
    data = response.json()
    
    assert data["user_id"] == user_id, "SessionMiddleware failed to set user_id"
    assert data["tier"] == "PAID", "EntitlementMiddleware failed to set tier"
    assert data["entitlement_stale"] is False, "Entitlement should not be stale"
    
    # 2. Test Stale Entitlement (Fail Closed)
    async def mock_redis_get_stale(key):
        if key == f"session:{sid}":
            return json.dumps({"user_id": user_id, "csrf": "abc"})
        if key == f"entitlement:user:{user_id}":
            # 10 minutes old (TTL is 5 mins)
            return json.dumps({
                "tier": "PAID", 
                "verified_at": int(time.time()) - 600 
            })
        return None
    
    redis_mock.get.side_effect = mock_redis_get_stale
    
    # Check debug endpoint (should show stale)
    response = client.get("/debug", cookies={"dd_session": sid})
    data = response.json()
    assert data["tier"] == "PAID" # It returns the tier from cache
    assert data["entitlement_stale"] is True # But marks it stale
    
    # Check Paid Route (should fail)
    response = client.get("/paid-only", cookies={"dd_session": sid})
    assert response.status_code == 401, "Should fail 401 on stale entitlement"
    
    # 3. Test Missing Entitlement (Fail Closed)
    async def mock_redis_get_miss(key):
        if key == f"session:{sid}":
            return json.dumps({"user_id": user_id, "csrf": "abc"})
        return None # No entitlement record
        
    redis_mock.get.side_effect = mock_redis_get_miss
    
    response = client.get("/paid-only", cookies={"dd_session": sid})
    assert response.status_code == 401, "Should fail 401 on missing entitlement (stale/unknown)"
    
    print("Middleware Order & Entitlement Logic Verified!")

if __name__ == "__main__":
    test_middleware_order_and_entitlement()
