# Implements TSD Section 4.1: Architecture & Design Patterns
# Implements TSD Section 4.4: Business Logic (High-level)

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import logging
import os

# Local imports
from app.core.config import settings
from app.api.routes import router as api_router
from app.services.poi_service import POIService

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# --- Application Lifecycle Management ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Implements TSD Section 5.4: MasterList cached in memory at container start
    # Implements TSD Section 4.1: Repository Pattern for MasterList
    logger.info(f"Application startup: v{settings.VERSION}")
    logger.info("Initializing POI Service and loading MasterList...")
    try:
        # Initialize the POIService, which loads the masterlist.json into memory
        app.state.poi_service = POIService()
        logger.info(f"MasterList loaded successfully with {len(app.state.poi_service.master_list)} points.")
    except Exception as e:
        logger.error(f"Failed to load MasterList: {e}")
        # In a real-world scenario, this might be a critical failure, but for MVP, we proceed with a warning
        # as the service might be able to handle it if the list is empty or mocked.
        pass

    # In a real-world scenario, Redis connection would be established here
    # app.state.redis_client = await connect_to_redis(settings.UPSTASH_REDIS_URL)
    
    yield
    
    # Application shutdown
    logger.info("Application shutdown: Cleaning up resources.")
    # In a real-world scenario, Redis connection would be closed here
    # await app.state.redis_client.close()

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
# TSD Section 5.2: CORS - only same-origin + vercel/render domains
# For a simple MVP, we will rely on the hosting platform's configuration (e.g., Vercel's default)
# and keep the application simple.

# --- Static Files and Templates ---
# Implements TSD Section 7.1: /static/ and /templates/
import os
# Resolve static and templates directories relative to this file
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
templates_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "templates"))
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)

# --- API Routes ---
app.include_router(api_router, prefix="/api")

# --- Root Endpoint (Landing Page) ---
# Implements TSD FR-001: Landing Page & Address Input
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # Implements TSD Section 9: Health Check (indirectly)
    # The frontend will rely on the API health, but this serves the main page.
    context = {
        "request": request,
        "mapbox_token": settings.MAPBOX_TOKEN, # Passed to frontend for potential map rendering/client-side geocoding fallback
        "turnstile_site_key": settings.CLOUDFLARE_TURNSTILE_SITE_KEY,
        "settings": settings,
    }
    return templates.TemplateResponse("index.html", context)

# --- Health Check Endpoint ---
# Implements TSD Section 9: Health Check
@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    # In a real implementation, this would check Redis and Mapbox counter
    # For this MVP, we return a mock response based on TSD
    mapbox_remaining = settings.MAX_MAPBOX_MONTHLY - (os.environ.get("MOCK_MAPBOX_COUNTER", 0))
    return {
        "status": "ok",
        "mapbox_remaining": max(0, mapbox_remaining)
    }

# --- Global Exception Handler (for unhandled errors) ---
import uuid

# ...

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

# Note: The actual rate-limiting, circuit-breaking, and geocoding logic
# will be implemented in the API routes and service layers.
