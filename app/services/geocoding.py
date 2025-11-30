# Implements TSD FR-004: Geocode User Address
# Implements TSD Section 4.1: Service Layer (geocoding_service)
# Implements TSD Section 6: Retry Logic (only for Mapbox)

import httpx
import logging
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

    query = address.strip()
    url = MAPBOX_API_URL.format(query=httpx.quote(query))
    
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
            logger.error(f"Unexpected error during geocoding: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ErrorResponse(
                    error="GEOCODING_INTERNAL_ERROR",
                    detail="An unexpected error occurred during geocoding."
                ).model_dump()
            )
    
    # Should be unreachable, but for completeness
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=ErrorResponse(
            error="GEOCODING_LOGIC_ERROR",
            detail="Geocoding logic failed to return a result."
        ).model_dump()
    )
