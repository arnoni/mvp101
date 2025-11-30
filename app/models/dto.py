# Implements TSD Section 4.2: Data Models
# Implements TSD Section 7.2: Type hints mandatory

from pydantic import BaseModel, Field
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

# --- Public Data Transfer Objects (DTOs) ---

class PublicPOIResult(BaseModel):
    """Public DTO for a single nearest POI result."""
    # Implements TSD Section 4.2: Public Response DTO
    name: str = Field(..., description="Name of the project.")
    distance_km: float = Field(..., description="Distance from user in kilometers.")
    google_maps_link: str = Field(..., description="Universal Google Maps navigation link.")
    image_url: str = Field(..., description="CDN URL for the project thumbnail.")
    lat: float = Field(..., description="Latitude of the project.")
    lon: float = Field(..., description="Longitude of the project.")

class FindNearestResponse(BaseModel):
    """Public DTO for the /api/find-nearest response."""
    # Implements TSD Section 4.2: Public Response DTO
    results: List[PublicPOIResult] = Field(..., description="Top 5 nearest POI results.")
    user_lat: float = Field(..., description="Geocoded latitude of the user's address.")
    user_lon: float = Field(..., description="Geocoded longitude of the user's address.")

# --- API Request Models ---

class FindNearestRequest(BaseModel):
    """Request model for the /api/find-nearest endpoint."""
    # Implements TSD Section 4.3: Request Body
    address: str = Field(..., description="User's free-text address.")
    turnstile_token: str = Field(..., description="Cloudflare Turnstile verification token.")

# --- Error Response Model ---

class ErrorResponse(BaseModel):
    """Standardized error response model."""
    # Implements TSD Section 4.3: Error Response Format
    error: str = Field(..., description="A machine-readable error code.")
    detail: str = Field(..., description="A human-readable explanation.")
    retry_after_seconds: Optional[int] = Field(None, description="Time until retry is allowed (for rate-limiting).")
