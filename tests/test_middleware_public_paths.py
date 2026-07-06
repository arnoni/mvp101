from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.middleware import EntitlementMiddleware
from app.middleware.identity import IdentityMiddleware
from app.middleware.public_paths import is_public_asset
from app.services.entitlement_service import EntitlementResult, TierStatus


def test_public_asset_predicate_allowlist_and_rejections():
    assert is_public_asset("/static/app.css") is True
    assert is_public_asset("/static/app.js") is True
    assert is_public_asset("/dilldrill_new_logo_2026.png") is True
    assert is_public_asset("/dd_icon.png") is True
    assert is_public_asset("/favicon.ico") is True
    assert is_public_asset("/sw.js") is True
    assert is_public_asset("/offline.html") is True
    assert is_public_asset("/") is False
    assert is_public_asset("/api/search") is False
    assert is_public_asset("/anything.css/private") is False
    assert is_public_asset("/wp-admin/install.php") is False
    assert is_public_asset("/static") is False
    assert is_public_asset("//static/x.css") is False
    assert is_public_asset("/static/../secret") is False


def _identity_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(IdentityMiddleware)

    @app.get("/{path:path}")
    async def catchall(request: Request):
        return {
            "identity_kind": getattr(request.state, "identity_kind", None),
            "identity_id": getattr(request.state, "identity_id", None),
        }

    return TestClient(app)


def _entitlement_client() -> TestClient:
    app = FastAPI()
    app.state.redis = AsyncMock()
    app.state.db_engine = object()
    app.add_middleware(EntitlementMiddleware)

    @app.get("/{path:path}")
    async def catchall(request: Request):
        return {"tier": getattr(request.state, "tier", None)}

    return TestClient(app)


def test_identity_skips_redis_for_static():
    mock_redis = AsyncMock()
    mock_redis.get.return_value = None
    mock_redis.hget = AsyncMock()

    with patch("app.services.redis_client.redis_client", mock_redis):
        response = _identity_client().get(
            "/static/app.css",
            cookies={settings.SESSION_COOKIE_NAME: "session-id"},
        )

    assert response.status_code == 200
    assert response.json() == {"identity_kind": None, "identity_id": None}
    mock_redis.get.assert_not_called()
    mock_redis.hget.assert_not_called()


def test_entitlement_skips_lookup_for_static():
    get_tier = AsyncMock(return_value=EntitlementResult(tier=TierStatus.FREE, daily_limit=2))

    with patch("app.core.middleware.EntitlementService.get_tier", get_tier):
        response = _entitlement_client().get("/static/app.css")

    assert response.status_code == 200
    assert response.json() == {"tier": None}
    get_tier.assert_not_called()


def test_entitlement_not_bypassed_for_api_search():
    get_tier = AsyncMock(return_value=EntitlementResult(tier=TierStatus.FREE, daily_limit=2))

    with patch("app.core.middleware.EntitlementService.get_tier", get_tier):
        response = _entitlement_client().get("/api/search")

    assert response.status_code == 200
    assert response.json() == {"tier": "FREE"}
    get_tier.assert_called_once()
