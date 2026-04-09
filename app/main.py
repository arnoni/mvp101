# Implements TSD Section 4.1: Architecture & Design Patterns
# Implements TSD Section 4.4: Business Logic (High-level)

from fastapi import FastAPI, Request, status, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import logging
import os
import uuid

# Local imports
from app.core.config import settings
from app.core.observability import init_sentry

# Configure Sentry as early as possible
init_sentry(
    dsn=settings.SENTRY_DSN,
    env=settings.ENV,
    release=settings.RELEASE or settings.VERSION
)

from app.services.poi_service import POIService
from app.logging import configure_logging
from app.middleware.logging import LoggingMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from upstash_redis.asyncio import Redis
from app.services.precompute_repo import PrecomputeRepository
from app.services.anomaly_service import AnomalyService
from app.services.demand_service import DemandService
from app.services.query_history_repository import QueryHistoryRepository
from app.services.plan_catalog_service import get_active_plan_prices

# Configure logging (Structlog)
configure_logging()
logger = logging.getLogger(__name__)

def build_async_engine() -> AsyncEngine:
    url = settings.DATABASE_URL
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    async_url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    # asyncpg incompatibility fix
    if "?sslmode=" in async_url or "&sslmode=" in async_url:
        async_url = async_url.replace("?sslmode=require", "").replace("&sslmode=require", "")
        async_url = async_url.replace("?channel_binding=require", "").replace("&channel_binding=require", "")

    if "neon.tech" in url and "-pooler.neon.tech" not in url:
        logger.warning("database_url_not_using_pooler")
    return create_async_engine(
        async_url,
        poolclass=NullPool,
        pool_pre_ping=True,
    )

# --- Application Lifecycle Management ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Application startup
    logger.info(f"Application startup: v{settings.VERSION}")
    logger.info("Initializing POI Service (PostGIS-backed)...")
    
    # Startup contract: Redis required in production when ENABLE_REDIS true
    # We now strictly use the REST client for stability on Vercel.
    rest_url = settings.UPSTASH_REDIS_REST_URL
    rest_token = settings.UPSTASH_REDIS_REST_TOKEN
    if settings.ENV == "production" and settings.ENABLE_REDIS and not (rest_url and rest_token):
        raise RuntimeError("ENABLE_REDIS=true requires UPSTASH_REDIS_REST_URL and TOKEN in production")
    
    # 1. Initialize POI Service
    try:
        app.state.db_engine = None
        if settings.DATABASE_URL:
            app.state.db_engine = build_async_engine()
        app.state.poi_service = POIService(app.state.db_engine)
        logger.info("POI Service initialized.")
    except Exception as e:
        logger.critical(f"Failed to initialize POI Service: {e}")
        # Let's ensure app.state.poi_service exists.
        class EmptyPOIService:
             master_list = []
             async def find_nearest_pois(self, *args, **kwargs): return [], ["POI Service failed to initialize"]
             async def get_pois_by_names(self, names): return []
        app.state.poi_service = EmptyPOIService()

    # 2. Initialize Redis & Quota Repository (REST client; stable in serverless)
    from app.services.quota_repository import QuotaRepository
    app.state.redis = None
    # Always ensure quota_repo exists to avoid AttributeErrors in PolicyEngine
    app.state.quota_repo = QuotaRepository(None) 
    
    # Debug: Check environment variables
    import os
    print("UPSTASH_REDIS_URL present:", bool(os.getenv("UPSTASH_REDIS_URL"))) 
    print("UPSTASH_REDIS_REST_URL present:", bool(os.getenv("UPSTASH_REDIS_REST_URL"))) 
    print("UPSTASH_REDIS_REST_TOKEN present:", bool(os.getenv("UPSTASH_REDIS_REST_TOKEN"))) 
    print("REST URL prefix:", (os.getenv("UPSTASH_REDIS_REST_URL") or "")[:40])

    # We prefer the REST client in Serverless (Vercel)
    rest_url = settings.UPSTASH_REDIS_REST_URL
    rest_token = settings.UPSTASH_REDIS_REST_TOKEN

    if settings.ENABLE_REDIS and rest_url and rest_token:
        try:
            app.state.redis = Redis(url=rest_url, token=rest_token)
            print("About to call redis.ping()")
            import asyncio
            pong = await asyncio.wait_for(app.state.redis.ping(), timeout=10)
            print("redis.ping() finished:", pong)
            if pong != "PONG":
                 # Upstash might return True or "PONG" depending on client
                 if not pong: raise RuntimeError("Redis ping failed")
                 
            app.state.quota_repo = QuotaRepository(app.state.redis)
            logger.info("Upstash Redis (REST) connected and QuotaRepository ready")
        except Exception as e:
            logger.error(f"Redis initialization failed: {e}")
            app.state.redis = None
    elif settings.ENABLE_REDIS:
        logger.error("ENABLE_REDIS set but UPSTASH_REDIS_REST_URL/TOKEN missing")

    # 3. Initialize MVP102 Services
    try:
        logger.info("Initializing MVP102 Services...")
        app.state.precompute_repo = PrecomputeRepository(app.state.db_engine)
        # Anomaly and Demand depend on Redis, but can handle None (no-op)
        app.state.anomaly_service = AnomalyService(app.state.redis)
        app.state.demand_service = DemandService(app.state.redis)
        app.state.query_history_repo = QueryHistoryRepository(app.state.db_engine, app.state.redis)
        logger.info("MVP102 Services initialized successfully.")
    except Exception as e:
        logger.critical(f"Failed to init MVP102 services: {e}", exc_info=True)

    logger.info("Lifespan startup complete.")


    yield
    
    # Application shutdown
    logger.info("Application shutdown: Cleaning up resources.")
    try:
        db_engine = getattr(app.state, "db_engine", None)
        if db_engine:
            await db_engine.dispose()
    except Exception:
        pass
    try:
        redis_cli = getattr(app.state, "redis", None)
        if redis_cli:
            await redis_cli.close()
    except Exception:
        pass


# 2. Create App
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=settings.BRIEF_DESCRIPTION,
    lifespan=lifespan,
    # Implements TSD Section 5.2: Headers
    docs_url=None,  # Disable docs for MVP security
    redoc_url=None, # Disable docs for MVP security
)

# --- Middleware and Exception Handlers ---
from app.middleware.identity import IdentityMiddleware
from app.core.middleware import EntitlementMiddleware
# LoggingMiddleware is imported above at line 20

# Order of precedence is BOTTOM to TOP for add_middleware in FastAPI
# For Identity (1st) -> Entitlement (2nd) -> Logging (3rd):
app.add_middleware(LoggingMiddleware)
app.add_middleware(EntitlementMiddleware)
app.add_middleware(IdentityMiddleware)

# --- Static Files and Templates ---
# Implements TSD Section 7.1: /static/ and /templates/
# Resolve static and templates directories relative to this file
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
templates_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "templates"))
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)

# --- API Routes ---
from app.api.routes import router as api_router
from app.api.auth import router as auth_router
from app.api.webhooks import router as webhooks_router
from app.api.billing import router as billing_router

app.include_router(api_router, prefix="/api")
app.include_router(auth_router, prefix="/api/auth")
app.include_router(webhooks_router, prefix="/api/webhooks")
app.include_router(billing_router, prefix="/api/billing")

#

@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    with open(os.path.join(static_dir, "privacy.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/sw.js", response_class=HTMLResponse)
async def service_worker():
    with open(os.path.join(static_dir, "sw.js"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), media_type="application/javascript")
        
@app.get("/offline.html", response_class=HTMLResponse)
async def offline():
    with open(os.path.join(static_dir, "offline.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.png", include_in_schema=False)
async def favicon():
    # To stop 404 spam in logs. In production, place a real favicon.png in /static
    target = os.path.join(static_dir, "favicon.png")
    if os.path.exists(target):
        from fastapi.responses import FileResponse
        return FileResponse(target)
    from fastapi.responses import Response
    return Response(status_code=204)

# --- Root Endpoint (Landing Page) ---
# Implements TSD FR-001: Landing Page & Address Input
@app.get("/", response_class=HTMLResponse)
async def root(request: Request, lang: str = "en"):
    # Implements TSD Section 12: I18n
    from app.services.i18n import get_translations, TRANSLATIONS, LANG_META
    from app.services.entitlement_service import TierStatus
    from app.services.policy_engine import PolicyEngine, RequestContext, PolicyVerdict, PolicyDecision
    
    using_fallback_quota = False
    try:
        quota_repo = getattr(request.app.state, "quota_repo", None)
        using_fallback_quota = not getattr(quota_repo, "redis_client", None)
    except Exception:
        using_fallback_quota = True
    
    # Prefer persisted language cookie if no explicit query override
    if not request.query_params.get("lang"):
        cookie_lang = request.cookies.get("dd_lang")
        if cookie_lang:
            lang = cookie_lang
    
    # Extract safely: if missing or explicitly None, fallback to "unknown_anon"
    anon_id = getattr(request.state, "anon_id", None) or "unknown_anon"
    tier = getattr(request.state, "tier", TierStatus.FREE)
    daily_limit = int(getattr(request.state, "daily_limit", 3) or 3)
    client_ip = request.client.host if request.client else None
    area_code = "global"
    policy_engine = PolicyEngine(request.app.state.quota_repo)
    context_eval = RequestContext(
        anon_id=anon_id,
        paid_tier=tier,
        area_code=area_code,
        client_ip=client_ip or "",
        turnstile_token=None,
        daily_limit=daily_limit,
    )
    decision = await policy_engine.evaluate(context_eval)
    limit = daily_limit
    can_search = decision.verdict != PolicyVerdict.BLOCK
    turnstile_required = decision.verdict == PolicyVerdict.CHALLENGE_REQUIRED
    quota_remaining = decision.quota_remaining
    checks_today = max(0, limit - quota_remaining)
    tdict = get_translations(lang)
    if not can_search:
        status_text = tdict.get("status_limit", "Daily limit reached")
        state = "limit"
    elif checks_today == 0:
        status_text = tdict.get("status_quiet", "Quiet check available")
        state = "quiet"
    elif checks_today == 1:
        status_text = tdict.get("status_active_one", "You’ve checked 1 place today")
        state = "active"
    else:
        status_text = tdict.get("status_active_many", "You’ve checked {n} places today").replace("{n}", str(checks_today))
        state = "active"
    tier_str = tier_to_client(tier)
    plan_prices = await get_active_plan_prices(getattr(request.app.state, "db_engine", None))
    
    context = {
        "request": request,
        "turnstile_site_key": settings.CLOUDFLARE_TURNSTILE_SITE_KEY,
        "settings": settings,
        "t": tdict,
        "t_all": TRANSLATIONS,
        "LANG_META": LANG_META,
        "current_lang": lang,
        "using_fallback_quota": using_fallback_quota,
        "initial_user_status": {"state": state, "text": status_text},
        "initial_can_search": can_search,
        "initial_turnstile_required": turnstile_required,
        "initial_checks_today": checks_today,
        "initial_tier": tier_str,
        "plan_prices": plan_prices,
    }
    return templates.TemplateResponse("index.html", context)

# --- Health Check Endpoint ---
# Implements TSD Section 9: Health Check
@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    # In a real implementation, this would check Redis
    return {
        "status": "ok",
    }

@app.get("/health/db", status_code=status.HTTP_200_OK)
async def db_health():
    db_engine = getattr(app.state, "db_engine", None)
    if not db_engine:
        raise HTTPException(status_code=503, detail="database not configured")
    try:
        async with db_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"db": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")

# --- Global Exception Handler (for unhandled errors) ---
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": {
                "error": "HTTP_ERROR",
                "detail": exc.detail,
                "status_code": exc.status_code
            }
        }
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_id = str(uuid.uuid4())
    logger.warning(f"VALIDATION_ERROR (ID: {error_id}): {exc.errors()}", extra={"url": str(request.url)})
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": {
                "error": "VALIDATION_ERROR",
                "detail": exc.errors(),
                "error_id": error_id
            }
        }
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_id = str(uuid.uuid4())
    
    # Extract more context for silent exceptions
    error_context = {
        "error_id": error_id,
        "method": request.method,
        "url": str(request.url),
        "client_ip": request.client.host if request.client else "unknown",
        "identity": getattr(request.state, "identity_id", "unknown") if hasattr(request.state, "identity_id") else "unknown",
        "exception_type": type(exc).__name__,
        "exception_msg": str(exc)
    }
    
    # Log with full context and stack trace
    logger.critical(f"GLOBAL_CRITICAL_FAILURE (ID: {error_id}): {exc}", extra=error_context, exc_info=True)
    
    # Integrate with Sentry explicitly for critical unhandled errors
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("error_id", error_id)
            scope.set_tag("exception_type", type(exc).__name__)
            for k, v in error_context.items():
                scope.set_extra(k, v)
            sentry_sdk.capture_exception(exc)
            # Ensure the exception is sent immediately
            sentry_sdk.flush()
    except Exception as sentry_err:
        logger.error(f"Failed to report to Sentry: {sentry_err}")

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": {
                "error": "INTERNAL_SERVER_ERROR",
                "detail": "An unexpected error occurred. Our team has been notified. Please provide the Error ID if you contact support.",
                "error_id": error_id
            }
        }
    )
def tier_to_client(tier):
    from app.services.entitlement_service import TierStatus
    if tier == TierStatus.PASS_3_DAY:
        return "3_day"
    if tier == TierStatus.PASS_1_DAY:
        return "1_day"
    return "free"
