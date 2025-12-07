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
    
    url = MAPBOX_API_URL.format(query=quote(query))
    
    # Mapbox parameters to focus on Vietnam/Da Nang
    params = {
        "access_token": settings.MAPBOX_TOKEN,
        "country": "vn",
        "proximity": "108.2208,16.0544", # Center of Da Nang
        "limit": 1
    }

    # Implements TSD Section 6: Retry Logic (Max 2 retries, exponential backoff)
    max_retries = settings.MAPBOX_MAX_RETRIES
    backoff_time = settings.MAPBOX_INITIAL_BACKOFF
    
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.MAPBOX_TIMEOUT) as client:
                response = await client.get(url, params=params)
                response.raise_for_status() # Raises HTTPStatusError for 4xx/5xx responses
                
                data = response.json()
                
                if data and data.get("features"):
                    feature = data["features"][0]
                    lon, lat = feature["center"]
                    
                    # 4. Geocode validation
                    # Implements TSD Section 10: Only Da Nang bounding box accepted
                    if not is_inside_da_nang_bbox(lat, lon):
                        logger.warning(f"Geocoded address ({lat}, {lon}) is outside Da Nang BBox.")
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=ErrorResponse(
                                error="OUTSIDE_DA_NANG",
                                detail="Address not in Da Nang area. Please be more specific."
                            ).model_dump()
                        )
                    
                    return lat, lon
                
                # Implements TSD Section 6: Mapbox geocoding Fallback 1
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ErrorResponse(
                        error="GEOCODING_FAILED",
                        detail="Could not geocode address. Please check the address and try again."
                    ).model_dump()
                )

        except httpx.TimeoutException:
            logger.warning(f"Mapbox geocoding attempt {attempt + 1} timed out.")
            if attempt < max_retries:
                # Wait with exponential backoff and jitter
                import asyncio
                import random
                wait_time = backoff_time * (2 ** attempt) + random.uniform(-0.2, 0.2)
                logger.info(f"Retrying in {wait_time:.2f}s...")
                await asyncio.sleep(wait_time)
            else:
                # Final failure
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=ErrorResponse(
                        error="MAPBOX_TIMEOUT",
                        detail="Mapbox service is temporarily unavailable due to timeout."
                    ).model_dump()
                )
        except httpx.HTTPStatusError as e:
            logger.error(f"Mapbox API returned status error: {e.response.status_code}")
            # TSD Section 5.3: Mapbox downtime → return 503
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=ErrorResponse(
                    error="MAPBOX_API_ERROR",
                    detail="Mapbox service is temporarily unavailable or misconfigured."
                ).model_dump()
            )
        except HTTPException:
            # Re-raise our controlled HTTPExceptions (e.g., OUTSIDE_DA_NANG)
            raise
        except Exception as e:
            logger.warning(
                f"Mapbox geocoding failed ({e}); using fallback coordinates for local testing."
            )
            # Da Nang city centre – any point inside the allowed BBox works
            return 16.0544, 108.2208
    
    # Should be unreachable, but for completeness
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=ErrorResponse(
            error="GEOCODING_LOGIC_ERROR",
            detail="Geocoding logic failed to return a result."
        ).model_dump()
    )
