# Implements TSD Section 7.2: Python 3.11+, pydantic 2+
# Implements TSD Section 9: Environment Variables (required)
# Implements TSD Section 10: Constraints & Limitations (Da Nang BBox)

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Any, Dict, List, Optional

class Settings(BaseSettings):
    # Implements TSD Section 5: Executive Summary
    PROJECT_NAME: str = "Da Nang Construction alert"
    VERSION: str = "0.1.1"
    BRIEF_DESCRIPTION: str = "A lightweight, serverless web application for finding the 5 nearest construction/real-estate points of interest in Da Nang, Vietnam."

    # --- Required Environment Variables (TSD Section 9) ---
    # MAPBOX_TOKEN: str = Field(..., description="Mapbox Geocoding API Token") # Disabled
    MAPBOX_TOKEN: Optional[str] = Field(None, description="Mapbox Geocoding API Token (Disabled)")
    CLOUDFLARE_TURNSTILE_SECRET: Optional[str] = Field(None, description="Cloudflare Turnstile Secret Key")
    CLOUDFLARE_TURNSTILE_SITE_KEY: Optional[str] = Field(None, description="Cloudflare Turnstile Site Key (Public)")
    REDIS_URL: Optional[str] = Field(None, description="Redis URL for quota/session enforcement")
    UPSTASH_REDIS_REST_URL: Optional[str] = Field(None, description="Upstash Redis REST URL")
    UPSTASH_REDIS_REST_TOKEN: Optional[str] = Field(None, description="Upstash Redis REST Token")
    SMOKE_TURNSTILE_TOKEN: Optional[str] = Field(None, description="Secure bypass token for Turnstile during smoke tests")
    ENV: str = Field("development", description="Application environment (e.g., production, development)")
    SENTRY_DSN: Optional[str] = Field(None, description="Sentry DSN for error reporting")
    RELEASE: Optional[str] = Field(None, description="Application release version for Sentry")
    MAX_MAPBOX_MONTHLY: int = Field(90000, description="Hard cap for Mapbox requests per month (TSD 10)")

    # --- Feature Flags (Impl Plan 2.1) ---
    ENABLE_REDIS: bool = Field(False, description="Feature flag for Redis")
    ENABLE_PAID_TIER: bool = Field(True, description="Feature flag for Paid Tier")
    SECRET_KEY: str = Field("dev_insecure_secret", description="Secret key for signing cookies")
    ADMIN_BYPASS_TOKEN: Optional[str] = Field(None, description="Signed admin token to bypass quotas/fallbacks")
    DATABASE_URL: Optional[str] = Field(None, description="PostgreSQL connection string (e.g., Neon) with sslmode=require")
    APP_ORIGIN: Optional[str] = Field(None, description="Allowed origin for CSRF checks, e.g. https://yourdomain.com")

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

    # I18n cache controls
    I18N_LRU_MAX: int = Field(8, description="Max cached languages in process memory", ge=1, le=64)
    I18N_WARMUP: bool = Field(True, description="Warm up translation cache at import time")

    # --- MVP102 New Settings ---
    
    # Identity & Session
    SESSION_COOKIE_NAME: str = "dd_session"
    SESSION_TTL_SECONDS: int = 604800  # 7 days
    ANON_COOKIE_NAME: str = "dd_anon"
    
    # Entitlement Cache (unified 10 minutes)
    ENTITLEMENT_CACHE_TTL_SECONDS: int = 600
    ENTITLEMENT_STALE_SECONDS: int = 300  # 5 min age threshold
    
    # Magic Link Security
    MAGICLINK_TOKEN_BYTES: int = 32
    MAGICLINK_EXPIRY_MINUTES: int = 30
    RESEND_API_KEY: Optional[str] = Field(None, description="Resend API Key for magic links")
    RESEND_FROM_EMAIL: str = "DillDrill <hello@onboarding-seg001.dilldrill.com>"
    RESEND_REPLY_TO: str = "dilldrillteam@gmail.com"
    
    # Merchant of Record (MoR) Settings
    LEMON_SQUEEZY_API_KEY: Optional[str] = Field(None, description="Lemon Squeezy API Key")
    PADDLE_API_KEY: Optional[str] = Field(None, description="Paddle API Key")
    DODO_API_KEY: Optional[str] = Field(None, description="Dodo Payments API Key")
    DODO_WEBHOOK_SECRET: Optional[str] = Field(None, description="Dodo webhook HMAC secret")
    
    # Quota (rolling windows, single-digit caps)
    QUOTA_FREE_ROLLING60_SOFT: int = 1
    QUOTA_FREE_ROLLING60_HARD: int = 2
    QUOTA_FREE_ROLLING24H_LIMIT: int = 2
    
    QUOTA_PAID_ROLLING60_SOFT: int = 2
    QUOTA_PAID_ROLLING60_HARD: int = 4
    QUOTA_PAID_ROLLING24H_LIMIT: int = 7
    
    # Precompute
    PRECOMPUTE_MIN_DEMAND: int = 5
    PRECOMPUTE_TOP_CELLS_LIMIT: int = 1000
    PRECOMPUTE_CELL_SIZES: List[int] = [55, 100, 200, 400]
    
    # Anomaly Detection
    ABUSE_SPIKE_MEDIUM: float = 3.0
    ABUSE_SPIKE_HIGH: float = 6.0
    ABUSE_REP_THRESHOLD_LOW: int = 4
    ABUSE_VELOCITY_TTL_SECONDS: int = 600
    ABUSE_SWEEP_TTL_SECONDS: int = 1800
    
    # Band thresholds (fractions of cell_size_m)
    BAND_IMMEDIATE_FRAC: float = 0.15
    BAND_NEARBY_FRAC: float = 0.35
    BAND_WITHIN_CELL_FRAC: float = 0.75

    # --- Legal Configuration ---
    LEGAL_EFFECTIVE_DATE: str = Field("17.04.2026", description="Effective and last-updated date shown in legal documents")
    LEGAL_CONTACT_EMAIL: str = Field("dilldrillteam@gmail.com", description="Contact email shown in legal documents")
    LEGAL_MINIMUM_AGE: str = Field("18", description="Minimum age shown in privacy policy")

    @property
    def LEGAL_CONFIG(self) -> Dict[str, str]:
        return {
            "date": self.LEGAL_EFFECTIVE_DATE,
            "contact_email": self.LEGAL_CONTACT_EMAIL,
            "minimum_age": self.LEGAL_MINIMUM_AGE,
        }

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
