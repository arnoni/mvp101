# Implements TSD Section 4.1: Architecture & Design Patterns
# Implements TSD Section 4.4: Business Logic (High-level)

from fastapi import FastAPI, Request, status, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import logging
import os
import httpx
import uuid

# Local imports
from app.core.config import settings
from app.services.poi_service import POIService

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Application Lifecycle Management ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Application startup
    logger.info(f"Application startup: v{settings.VERSION}")
    logger.info("Initializing POI Service and loading MasterList...")
    
    # 1. Initialize POI Service
    # 1. Initialize POI Service
    try:
        app.state.poi_service = POIService()
        logger.info(f"MasterList loaded successfully with {len(app.state.poi_service.master_list)} points.")
    except Exception as e:
        logger.critical(f"Failed to initialize POI Service: {e}")
        # Initialize empty to prevent crashes downstream
        from app.services.poi_service import POIService
        # We need to construct it manually if init failed? 
        # Actually POIService.__init__ catches its own errors and sets master_list=[].
        # So this outer try/catch is redundant if POIService handles it.
        # But if POIService.__init__ RAISES, we catch here.
        # Let's ensure app.state.poi_service exists.
        class EmptyPOIService:
             master_list = []
             def find_nearest_pois(self, *args, **kwargs): return []
        app.state.poi_service = EmptyPOIService()

    # 2. Initialize Redis & Quota Repository
    if settings.ENABLE_REDIS:
        from app.services.redis_client import RealRedisClient
        from app.services.quota_repository import QuotaRepository
        try:
             # Create global Redis client
             redis_client = RealRedisClient(settings.UPSTASH_REDIS_URL)
             app.state.redis_client = redis_client
             
             # Create Quota Repo
             app.state.quota_repo = QuotaRepository(redis_client)
             logger.info("QuotaRepository initialized with Real Redis.")
        except Exception as e:
             logger.error(f"Failed to connect to Redis: {e}. Degrading to in-memory quota.")
             from app.services.quota_repository import QuotaRepository
             app.state.quota_repo = QuotaRepository(None)
    else:
         logger.info("Redis disabled via config. Using in-memory quota.")
         from app.services.quota_repository import QuotaRepository
         app.state.quota_repo = QuotaRepository(None)

    yield
    
    # Application shutdown
    logger.info("Application shutdown: Cleaning up resources.")
    if hasattr(app.state, "redis_client") and app.state.redis_client:
         await app.state.redis_client._redis.aclose() # Access internal redis to close


# --- FastAPI Application Initialization ---
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
from app.core.middleware import AnonIdMiddleware
app.add_middleware(AnonIdMiddleware)

# --- Static Files and Templates ---
# Implements TSD Section 7.1: /static/ and /templates/
# Resolve static and templates directories relative to this file
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
templates_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "templates"))
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)

# --- API Routes ---
from app.api.routes import router as api_router
app.include_router(api_router, prefix="/api")

# --- Turnstile Verification Endpoint (Legacy/Backup?) ---
# Note: Verification logic is now mostly in app.utils.security and PolicyEngine
# But we can keep this for direct frontend testing if needed.
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

@app.post("/api/turnstile/verify")
async def api_turnstile_verify(request: Request):
    body = await request.json()
    token = body.get("token")
    if not token:
        raise HTTPException(status_code=400, detail="Missing Turnstile token")

    # Access secret directly from settings (loaded from env)
    secret = settings.CLOUDFLARE_TURNSTILE_SECRET
    if not secret:
        raise HTTPException(status_code=500, detail="Turnstile secret not configured")

    # optional: include user IP
    client_ip = request.client.host if request.client else None

    data = {"secret": secret, "response": token}
    if client_ip:
        data["remoteip"] = client_ip

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(TURNSTILE_VERIFY_URL, data=data)
        result = r.json()

    if not result.get("success"):
        # Useful during debugging; you can log result.get("error-codes")
        raise HTTPException(status_code=403, detail=result)

    return {"ok": True}

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

# --- Root Endpoint (Landing Page) ---
# Implements TSD FR-001: Landing Page & Address Input
@app.get("/", response_class=HTMLResponse)
async def root(request: Request, lang: str = "en"):
    # Implements TSD Section 12: I18n
    from app.services.i18n import get_translations
    
    context = {
        "request": request,
        "turnstile_site_key": settings.CLOUDFLARE_TURNSTILE_SITE_KEY,
        "settings": settings,
        "t": get_translations(lang),
        "current_lang": lang
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

# --- Global Exception Handler (for unhandled errors) ---
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_id = str(uuid.uuid4())
    logger.error(f"Unhandled exception (ID: {error_id}): {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": {
                "error": "INTERNAL_SERVER_ERROR",
                "detail": "An unexpected error occurred. Please report this error ID.",
                "error_id": error_id
            }
        }
    )
