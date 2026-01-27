# Implements TSD Section 7.2: Python 3.11+, pydantic 2+
# Implements TSD Section 9: Environment Variables (required)
# Implements TSD Section 10: Constraints & Limitations (Da Nang BBox)

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import List, Optional

class Settings(BaseSettings):
    # Implements TSD Section 5: Executive Summary
    PROJECT_NAME: str = "Da Nang Construction alert"
    VERSION: str = "0.1.1"
    BRIEF_DESCRIPTION: str = "A lightweight, serverless web application for finding the 5 nearest construction/real-estate points of interest in Da Nang, Vietnam."

    # --- Required Environment Variables (TSD Section 9) ---
    # MAPBOX_TOKEN: str = Field(..., description="Mapbox Geocoding API Token") # Disabled
    MAPBOX_TOKEN: Optional[str] = Field(None, description="Mapbox Geocoding API Token (Disabled)")
    CLOUDFLARE_TURNSTILE_SECRET: str = Field(..., description="Cloudflare Turnstile Secret Key")
    CLOUDFLARE_TURNSTILE_SITE_KEY: str = Field(..., description="Cloudflare Turnstile Site Key (Public)")
    UPSTASH_REDIS_REST_URL: str = Field(..., description="URL for Upstash Redis REST API")
    UPSTASH_REDIS_REST_TOKEN: str = Field(..., description="Token for Upstash Redis REST API")
    ENV: str = Field("development", description="Application environment (e.g., production, development)")
    MAX_MAPBOX_MONTHLY: int = Field(90000, description="Hard cap for Mapbox requests per month (TSD 10)")

    # --- Feature Flags (Impl Plan 2.1) ---
    ENABLE_REDIS: bool = Field(True, description="Feature flag for Redis")
    ENABLE_PAID_TIER: bool = Field(True, description="Feature flag for Paid Tier")
    ADMIN_BYPASS_TOKEN: Optional[str] = Field(None, description="Signed admin token to bypass quotas/fallbacks")

    # --- Constraints & Limitations (TSD Section 10) ---
    # Da Nang Bounding Box: 16.00–16.12 N, 108.10–108.30 E
    DA_NANG_BBOX: List[float] = Field(
        [108.10, 16.00, 108.30, 16.12], # [min_lon, min_lat, max_lon, max_lat]
        description="Bounding box for Da Nang area [lon_min, lat_min, lon_max, lat_max]"
    )
    
    # --- Other Settings ---
    # Rate limit in seconds (1 req/IP/24h = 86400 seconds)
    RATE_LIMIT_SECONDS: int = 86400
    
    # Mapbox Retry Logic (TSD 6)
    MAPBOX_MAX_RETRIES: int = 2
    MAPBOX_INITIAL_BACKOFF: float = 1.0 # seconds
    
    # Timeout for Mapbox Geocoding (TSD 6)
    MAPBOX_TIMEOUT: int = 8 # seconds

    # Search Radius (TSD FR-005 modified)
    SEARCH_RADIUS_KM: float = 0.1 # 100 meters

    # Pydantic configuration
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"
    )

settings = Settings()

def is_inside_da_nang_bbox(lat: float, lon: float) -> bool:
    """Implements TSD Section 10: Only Da Nang bounding box accepted."""
    min_lon, min_lat, max_lon, max_lat = settings.DA_NANG_BBOX
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon
