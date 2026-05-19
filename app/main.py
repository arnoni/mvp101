# Implements TSD Section 4.1: Architecture & Design Patterns
# Implements TSD Section 4.4: Business Logic (High-level)

from fastapi import FastAPI, Request, status, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import os
import uuid
import secrets
import structlog
import httpx

# Local imports
from app.core.config import settings
from app.core.db import create_asyncpg_engine
from app.core.observability import get_vercel_context, init_sentry, report_exception
from app.core.security_startup import validate_startup_security_settings

# Configure Sentry as early as possible
init_sentry(
    dsn=settings.SENTRY_DSN,
    env=settings.ENV,
    release=settings.RELEASE or settings.VERSION
)

from app.services.poi_service import POIService
from app.logging import configure_logging
from app.middleware.logging import LoggingMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine
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
logger = structlog.get_logger(__name__)
_POOLER_WARNING_EMITTED = False


def _build_content_security_policy(nonce: str) -> str:
    """Build the production CSP used by the FastAPI app.

    The hero search page loads PostHog, Sentry Browser, and Cloudflare
    Turnstile. Keep these hosts in sync with templates/index.html and
    static/app.js so security headers do not silently block telemetry or
    verification callbacks.
    """
    directives = {
        "default-src": ["'self'"],
        "base-uri": ["'self'"],
        "object-src": ["'none'"],
        "frame-ancestors": ["'self'"],
        "form-action": ["'self'"],
        "script-src": [
            "'self'",
            f"'nonce-{nonce}'",
            "https://browser.sentry-cdn.com",
            "https://challenges.cloudflare.com",
            "https://*.i.posthog.com",
            "https://cdn.tailwindcss.com",
        ],
        "connect-src": [
            "'self'",
            "https://challenges.cloudflare.com",
            "https://*.i.posthog.com",
            "https://*.posthog.com",
            "https://*.sentry.io",
            "https://*.ingest.sentry.io",
        ],
        "frame-src": ["https://challenges.cloudflare.com"],
        "img-src": ["'self'", "data:", "blob:", "https:"],
        "style-src": ["'self'", "'unsafe-inline'"],
        "font-src": ["'self'", "data:"],
        "worker-src": ["'self'", "blob:"],
    }
    return "; ".join(f"{key} {' '.join(values)}" for key, values in directives.items())


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.csp_nonce = secrets.token_urlsafe(16)
        try:
            response = await call_next(request)
        except Exception as exc:
            if not getattr(request.state, "error_reported", False):
                request.state.error_reported = True
                report_exception(
                    exc,
                    event="security_headers_downstream_failed",
                    logger=logger,
                    request_id=getattr(request.state, "request_id", None),
                    path=request.url.path,
                    method=request.method,
                    vercel_id=getattr(request.state, "vercel_id", None),
                )
            raise
        try:
            response.headers["Content-Security-Policy"] = _build_content_security_policy(request.state.csp_nonce)
            if "X-Content-Type-Options" not in response.headers:
                response.headers["X-Content-Type-Options"] = "nosniff"
        except Exception as exc:
            report_exception(
                exc,
                event="security_headers_apply_failed",
                logger=logger,
                request_id=getattr(request.state, "request_id", None),
                path=request.url.path,
                method=request.method,
                vercel_id=getattr(request.state, "vercel_id", None),
            )
            raise
        return response

def build_async_engine() -> AsyncEngine:
    global _POOLER_WARNING_EMITTED
    url = settings.DATABASE_URL
    if not url:
        raise RuntimeError("DATABASE_URL is not set")

    if "neon.tech" in url and "-pooler.neon.tech" not in url and not _POOLER_WARNING_EMITTED:
        logger.warning("database_url_not_using_pooler")
        _POOLER_WARNING_EMITTED = True
    return create_asyncpg_engine(
        url,
        poolclass=NullPool,
        pool_pre_ping=True,
    )

# --- Application Lifecycle Management ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Application startup
    logger.info("application_startup", version=settings.VERSION, vercel=get_vercel_context())
    validate_startup_security_settings(settings)
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
            try:
                async with app.state.db_engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                    await conn.commit()
                logger.info("Database connectivity check passed.")
            except Exception as e:
                # Do NOT raise — let the app start so health checks work.
                # But report it through the resilient observability path so it cannot disappear.
                report_exception(
                    e,
                    event="database_connectivity_check_failed",
                    logger=logger,
                    flush=settings.ENV == "production",
                )
        else:
            logger.warning("DATABASE_URL not set — running without database connectivity.")
        app.state.poi_service = POIService(app.state.db_engine)
        logger.info("POI Service initialized.")
    except Exception as e:
        report_exception(e, event="poi_service_init_failed", logger=logger, flush=settings.ENV == "production")
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
    
    logger.debug(
        "redis_env_presence",
        upstash_redis_url_present=bool(os.getenv("UPSTASH_REDIS_URL")),
        upstash_redis_rest_url_present=bool(os.getenv("UPSTASH_REDIS_REST_URL")),
        upstash_redis_rest_token_present=bool(os.getenv("UPSTASH_REDIS_REST_TOKEN")),
    )

    # We prefer the REST client in Serverless (Vercel)
    rest_url = settings.UPSTASH_REDIS_REST_URL
    rest_token = settings.UPSTASH_REDIS_REST_TOKEN

    if settings.ENABLE_REDIS and rest_url and rest_token:
        try:
            app.state.redis = Redis(url=rest_url, token=rest_token)
            import asyncio
            pong = await asyncio.wait_for(app.state.redis.ping(), timeout=10)
            if pong != "PONG":
                # Upstash might return True or "PONG" depending on client
                if not pong:
                    raise RuntimeError("Redis ping failed")

            app.state.quota_repo = QuotaRepository(app.state.redis)
            await app.state.quota_repo.load_lua_scripts()
            try:
                from app.api.auth import load_anon_quota_carry_forward_script

                await load_anon_quota_carry_forward_script(app.state.redis)
            except Exception as script_err:
                report_exception(
                    script_err,
                    event="anon_quota_carry_forward_script_load_failed",
                    logger=logger,
                    flush=settings.ENV == "production",
                )
            logger.info("Upstash Redis (REST) connected and QuotaRepository ready")
        except Exception as e:
            report_exception(e, event="redis_initialization_failed", logger=logger, flush=settings.ENV == "production")
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
        report_exception(e, event="mvp102_services_init_failed", logger=logger, flush=settings.ENV == "production")
    app.state.maps_http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(8.0, connect=3.0),
        follow_redirects=True,
        max_redirects=8,
    )

    logger.info("Lifespan startup complete.")


    yield
    
    # Application shutdown
    logger.info("Application shutdown: Cleaning up resources.")
    try:
        db_engine = getattr(app.state, "db_engine", None)
        if db_engine:
            await db_engine.dispose()
    except Exception as exc:
        report_exception(
            exc,
            event="E_APP_SHUTDOWN_DB_DISPOSE_FAILED",
            logger=logger,
            event_code="E_APP_SHUTDOWN_DB_DISPOSE_FAILED",
        )
    try:
        redis_cli = getattr(app.state, "redis", None)
        if redis_cli:
            await redis_cli.close()
    except Exception as exc:
        report_exception(
            exc,
            event="E_APP_SHUTDOWN_REDIS_CLOSE_FAILED",
            logger=logger,
            event_code="E_APP_SHUTDOWN_REDIS_CLOSE_FAILED",
        )
    try:
        maps_http_client = getattr(app.state, "maps_http_client", None)
        if maps_http_client:
            await maps_http_client.aclose()
    except Exception as exc:
        report_exception(
            exc,
            event="E_APP_SHUTDOWN_MAPS_HTTP_CLIENT_CLOSE_FAILED",
            logger=logger,
            event_code="E_APP_SHUTDOWN_MAPS_HTTP_CLIENT_CLOSE_FAILED",
        )


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
# For Identity (1st) -> Entitlement (2nd) -> Logging (3rd) -> Security headers (wrap response):
app.add_middleware(SecurityHeadersMiddleware)
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

@app.get("/legal", response_class=HTMLResponse)
async def legal_hub(request: Request):
    return templates.TemplateResponse("legal/index.html", {"request": request, "lang": request.cookies.get("dd_lang", "en"), "legal_config": settings.LEGAL_CONFIG})

@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse("legal/terms.html", {"request": request, "lang": request.cookies.get("dd_lang", "en"), "legal_config": settings.LEGAL_CONFIG})

@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse("legal/privacy.html", {"request": request, "lang": request.cookies.get("dd_lang", "en"), "legal_config": settings.LEGAL_CONFIG})

@app.get("/research-notice", response_class=HTMLResponse)
async def research_notice(request: Request):
    return templates.TemplateResponse("legal/research_notice.html", {"request": request, "lang": request.cookies.get("dd_lang", "en"), "legal_config": settings.LEGAL_CONFIG})

@app.get("/research-access", response_class=HTMLResponse)
async def research_access(request: Request):
    return templates.TemplateResponse("legal/research_access.html", {"request": request, "lang": request.cookies.get("dd_lang", "en"), "legal_config": settings.LEGAL_CONFIG})

@app.get("/sw.js", response_class=HTMLResponse)
async def service_worker(request: Request):
    try:
        with open(os.path.join(static_dir, "sw.js"), "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), media_type="application/javascript")
    except Exception as exc:
        report_exception(
            exc,
            event="service_worker_read_failed",
            logger=logger,
            request_id=getattr(request.state, "request_id", None),
            path=request.url.path,
        )
        raise HTTPException(status_code=500, detail="service worker unavailable") from exc
        
@app.get("/offline.html", response_class=HTMLResponse)
async def offline(request: Request):
    try:
        with open(os.path.join(static_dir, "offline.html"), "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    except Exception as exc:
        report_exception(
            exc,
            event="offline_page_read_failed",
            logger=logger,
            request_id=getattr(request.state, "request_id", None),
            path=request.url.path,
        )
        raise HTTPException(status_code=500, detail="offline page unavailable") from exc

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
    except Exception as exc:
        using_fallback_quota = True
        report_exception(
            exc,
            event="root_quota_fallback_detection_failed",
            logger=logger,
            request_id=getattr(request.state, "request_id", None),
            path=request.url.path,
        )
    
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
    # Reset Turnstile cache on new page load per user request
    try:
        from app.utils.security import _turnstile_cache_key
        cache_key = _turnstile_cache_key(
            anon_id=anon_id if anon_id != "unknown_anon" else None, 
            client_ip=client_ip, 
            user_agent=request.headers.get("user-agent")
        )
        redis_cli = getattr(request.app.state, "redis", None)
        if redis_cli and cache_key:
            await redis_cli.delete(cache_key)
    except Exception as exc:
        logger.warning("failed_to_clear_turnstile_cache_on_pageload", error=str(exc))

    try:
        decision = await policy_engine.evaluate(context_eval)
    except Exception as exc:
        report_exception(
            exc,
            event="root_policy_evaluate_failed",
            logger=logger,
            request_id=getattr(request.state, "request_id", None),
            anon_id=anon_id,
            tier=str(tier),
        )
        decision = PolicyDecision(verdict=PolicyVerdict.CHALLENGE_REQUIRED, quota_remaining=0, max_results=1)
        using_fallback_quota = True
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
    try:
        plan_prices = await get_active_plan_prices(getattr(request.app.state, "db_engine", None))
    except Exception as exc:
        report_exception(
            exc,
            event="root_plan_prices_load_failed",
            logger=logger,
            request_id=getattr(request.state, "request_id", None),
            path=request.url.path,
        )
        plan_prices = {}
    
    context = {
        "request": request,
        "user_id": getattr(request.state, "user_id", None),
        "anon_id": str(request.state.anon_id) if getattr(request.state, "anon_id", None) else "",
        "turnstile_site_key": settings.CLOUDFLARE_TURNSTILE_SITE_KEY,
        "posthog_key": os.environ.get("POSTHOG_PROJECT_API_KEY", ""),
        "posthog_host": os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"),

        # Frontend observability  
        "sentry_frontend_dsn": os.environ.get("SENTRY_FRONTEND_DSN") or settings.SENTRY_DSN or "",
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "sentry_env": settings.ENV,
        "sentry_release": settings.RELEASE or settings.VERSION,

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
        "initial_daily_limit": daily_limit,
        "initial_tier": tier_str,
        "tier": tier_str,
        "demand_allowed": tier in {TierStatus.SIMULATED_PAID, TierStatus.PASS_1_DAY, TierStatus.PASS_3_DAY},
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
    except Exception as exc:
        report_exception(exc, event="db_health_check_failed", logger=logger, path="/health/db")
        raise HTTPException(status_code=503, detail="database unavailable") from exc

# --- Global Exception Handler (for unhandled errors) ---
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    error_id = str(uuid.uuid4())
    detail_payload = exc.detail
    if isinstance(exc.detail, dict):
        detail_payload = exc.detail.get("detail", exc.detail)
        error_id = str(exc.detail.get("error_id") or error_id)

    if exc.status_code >= 500:
        report_exception(
            exc,
            event="http_exception_5xx",
            logger=logger,
            error_id=error_id,
            status_code=exc.status_code,
            detail=detail_payload,
            url=str(request.url),
            request_id=getattr(request.state, "request_id", None),
        )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": {
                "error": "HTTP_ERROR",
                "detail": detail_payload,
                "status_code": exc.status_code,
                "error_id": error_id,
            }
        }
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    error_id = str(uuid.uuid4())
    logger.warning(
        "validation_error",
        error_id=error_id,
        errors=exc.errors(),
        url=str(request.url),
        request_id=getattr(request.state, "request_id", None),
    )
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
    
    if not getattr(request.state, "error_reported", False):
        report_exception(
            exc,
            event="global_critical_failure",
            logger=logger,
            flush=settings.ENV == "production",
            request_id=getattr(request.state, "request_id", None),
            vercel_id=getattr(request.state, "vercel_id", None),
            **error_context,
        )
        request.state.error_reported = True
    else:
        logger.critical("global_critical_failure_already_reported", **error_context)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": {
                "error": "HTTP_ERROR",
                "detail": "An unexpected error occurred. Our team has been notified. Please provide the Error ID if you contact support.",
                "status_code": status.HTTP_500_INTERNAL_SERVER_ERROR,
                "error_id": error_id
            }
        }
    )
def tier_to_client(tier):
    from app.services.entitlement_service import TierStatus
    if tier == TierStatus.SIMULATED_PAID:
        return "simulated_paid"
    if tier == TierStatus.PASS_3_DAY:
        return "3_day"
    if tier == TierStatus.PASS_1_DAY:
        return "1_day"
    return "free"
