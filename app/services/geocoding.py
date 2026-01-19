# Implements TSD FR-004: Geocode User Address
# Implements TSD Section 4.1: Service Layer (geocoding_service)
# Implements TSD Section 6: Retry Logic (only for Mapbox)

import httpx
import logging
import re
from urllib.parse import quote
from typing import Optional, Tuple
from fastapi import HTTPException, status
from app.core.config import settings, is_inside_da_nang_bbox
from app.models.dto import ErrorResponse

logger = logging.getLogger(__name__)

MAPBOX_API_URL = "https://api.mapbox.com/geocoding/v5/mapbox.places/{query}.json"

async def geocode_address(address: str) -> Tuple[float, float]:
    """
    Geocodes a free-text address using the Mapbox Geocoding API.
    Implements TSD FR-004.
    
    Returns:
        A tuple of (latitude, longitude).
    
    Raises:
        HTTPException: If geocoding fails or the address is outside the Da Nang area.
    """
    # Implements TSD Section 8: Validate and sanitize all inputs
    if not address or len(address.strip()) < 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="INVALID_ADDRESS",
                detail="Address must be provided and be at least 5 characters long."
            ).model_dump()
        )

    # Clean input
    query = address.strip()

    # ---------------------------------------------------------
    # Coordinate Input Support
    # ---------------------------------------------------------
    # Regex for "lat,lon" or "lat, lon" or "lon, lat"
    # Matches simple float patterns: START +/-digits.digits , +/-digits.digits END
    # Updated to support comma as decimal separator (European style) and loose spacing
    coord_pattern = r'^([-+]?\d{1,3}(?:[.,]\d+)?)[,\s]+([-+]?\d{1,3}(?:[.,]\d+)?)$'
    match = re.match(coord_pattern, query)
    
    if match:
        try:
            # Normalized values: replace comma decimal with dot
            s_val1 = match.group(1).replace(',', '.')
            s_val2 = match.group(2).replace(',', '.')
            
            val1 = float(s_val1)
            val2 = float(s_val2)
            
            # Simple heuristic to distinguish Lat vs Lon if not explicitly clear:
            # Lat is usually [-90, 90], Lon is [-180, 180].
            # In Vietnam (Da Nang), Lat is ~16, Lon is ~108.
            
            # Case 1: Lat, Lon (e.g. 16.06, 108.23)
            # Standard Lat range: -90 to 90. Standard Lon range: -180 to 180.
            if abs(val1) <= 90 and abs(val2) <= 180:
                # Ambiguous if both are small (e.g. 10, 10). 
                # Prefer matches that fit into Da Nang range ~16, ~108
                if 15 <= val1 <= 17 and 107 <= val2 <= 109:
                     lat, lon = val1, val2
                elif 15 <= val2 <= 17 and 107 <= val1 <= 109:
                     lat, lon = val2, val1
                else:
                    # Default to Lat, Lon
                     lat, lon = val1, val2
            else:
                # If val1 > 90, it must be Lon.
                if abs(val1) > 90:
                    lon, lat = val1, val2
                else:
                    lat, lon = val1, val2 # Fallback

            # Use existing BBox validation
            if is_inside_da_nang_bbox(lat, lon):
                logger.info(f"Direct coordinate input detected and VALID: {lat}, {lon}")
                return lat, lon
            else:
                 logger.warning(f"Direct coordinate input {lat},{lon} is outside Da Nang BBox.")
                 raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ErrorResponse(
                        error="OUTSIDE_DA_NANG",
                        detail=f"Coordinates {lat},{lon} are outside the supported Da Nang area."
                    ).model_dump()
                 )

        except ValueError as e:
            logger.error(f"Float conversion error: {e}")
            pass # Fallthrough to text geocoding
    
    # Mapbox logic disabled by user request.
    # url = MAPBOX_API_URL.format(query=quote(query))
    
    # Return error for text addresses since Geocoding is disabled
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorResponse(
            error="GEOCODING_DISABLED",
            detail="Address search is disabled. Please enter coordinates (Lat, Lon) directly."
        ).model_dump()
    )
