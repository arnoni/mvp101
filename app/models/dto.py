# Implements TSD Section 4.2: Data Models
# Implements TSD Section 7.2: Type hints mandatory

from pydantic import BaseModel, Field, HttpUrl, ConfigDict, model_validator
from typing import List, Optional

# --- Internal Data Models (MasterList) ---

class POI(BaseModel):
    """Internal Point of Interest model from MasterList.json."""
    id: str = Field(..., description="Unique site identifier.")
    name: str = Field(..., description="Public name of the project.")
    lat: float = Field(..., description="Latitude.")
    lon: float = Field(..., description="Longitude.")
    images: List[str] = Field(default_factory=list, description="List of image filenames.")
    internal_notes: Optional[str] = Field(None, description="Private notes (never exposed).")

class MasterList(BaseModel):
    """Root model for MasterList.json."""
    points: List[POI]

# --- Precompute Models ---

class PrecomputeCandidate(BaseModel):
    """Raw candidate from the DB precompute JSON."""
    lat: float
    lon: float
    category: str
    name_hash: Optional[str] = None # For dedupe, not display
    metadata: dict = {}

# --- Public Data Transfer Objects (DTOs) ---

class ReportLine(BaseModel):
    """Opaque display line for the user. No precise coords."""
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., description="The rendered report line (e.g. 'Coffee Shop (~250m)')")
    category: str = Field("unknown", description="Icon category")

class UserStatus(BaseModel):
    state: str = Field(..., description="quiet|active|limit")
    text: str = Field(..., description="Localized fallback text")

class FindNearestResponse(BaseModel):
    """Public DTO for the /api/find-nearest response. SAFE/OPAQUE."""
    # Implements TSD Section 4.2: Public Response DTO
    report_lines: List[ReportLine] = Field(default_factory=list, description="Opaque result lines.")
    
    # We DO NOT return user_lat/lon anymore to avoid mirroring precise input if possible, 
    # but for UI map centering we might need it.
    # Plan says "Opaque report lines only".
    # If we return user_lat/lon, it's just what they sent us.
    # But let's remove it to be strict, or keep it if UI needs it.
    # UI "Update UI Distance and Map" (historical) wanted it.
    # Let's keep it for now but remove POI coords.
    user_lat: float = Field(..., description="Geocoded latitude.")
    user_lon: float = Field(..., description="Geocoded longitude.")
    
    quota_remaining: int = Field(..., description="Remaining daily quota.")
    share_url: Optional[str] = Field(None, description="Shareable URL for this search.")
    debug_logs: Optional[List[str]] = Field(None, description="Debug logs for dev mode.")
    user_status: UserStatus = Field(..., description="User-facing status object.")
    can_search: bool = Field(..., description="Whether a search can proceed.")
    turnstile_required: bool = Field(..., description="Whether Turnstile is required for this request.")
    checks_today: int = Field(..., description="Number of checks performed today.")
    tier: Optional[str] = Field(None, description="free|pro|null")
    results_state: str = Field(..., description="never|empty|found")
    errors: Optional["ErrorResponse"] = Field(None, description="Optional non-blocking error envelope.")

# --- API Request Models ---

class FindNearestRequest(BaseModel):
    """Request model for the /api/find-nearest endpoint."""
    # Implements TSD Section 4.3: Request Body
    # Implements TSD v1.1: Direct Lat/Lng input
    lat: Optional[float] = Field(default=None, description="Latitude of the user/search center.")
    lon: Optional[float] = Field(default=None, description="Longitude of the user/search center.")
    turnstile_token: Optional[str] = Field(None, description="Cloudflare Turnstile verification token (optional).")
    location_input: Optional[str] = Field(default=None, max_length=2048)
    input_kind_hint: Optional[str] = Field(default=None, max_length=64)
    client_parsed_lat: Optional[float] = Field(default=None)
    client_parsed_lng: Optional[float] = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def fill_legacy_lat_lon(cls, values):
        if not isinstance(values, dict):
            return values
        if values.get("lat") is None and values.get("client_parsed_lat") is not None:
            values["lat"] = values["client_parsed_lat"]
        if values.get("lon") is None and values.get("client_parsed_lng") is not None:
            values["lon"] = values["client_parsed_lng"]
        return values

# --- Error Response Model ---

class ErrorResponse(BaseModel):
    """Standardized error response model."""
    # Implements TSD Section 4.3: Error Response Format
    error: str = Field(..., description="A machine-readable error code.")
    detail: str = Field(..., description="A human-readable explanation.")
    retry_after_seconds: Optional[int] = None
    quota_remaining: Optional[int] = None
    error_id: Optional[str] = None

class StatusResponse(BaseModel):
    user_status: UserStatus
    can_search: bool
    turnstile_required: bool
    checks_today: int
    tier: Optional[str] = None
