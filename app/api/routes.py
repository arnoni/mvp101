# Implements TSD Section 4.3: API Specifications
# Implements TSD Section 4.4: Business Logic (Pseudocode)
# Implements TSD Section 4.1: Service Layer (geocoding_service, poi_service, kmz_service)

from fastapi import APIRouter, Request, HTTPException, status, Depends
from fastapi.responses import JSONResponse, Response
import logging
import time
from typing import List

# External dependencies (mocked for now, but required for real implementation)
# import redis.asyncio as redis # For Upstash Redis
# from redis.asyncio.client import Redis

# Local imports
from app.core.config import settings
from app.models.dto import FindNearestRequest, FindNearestResponse, ErrorResponse
from app.utils.security import verify_turnstile, get_client_ip
from app.services.geocoding import geocode_address
from app.services.poi_service import POIService
from app.services.kmz_service import generate_kmz

logger = logging.getLogger(__name__)
router = APIRouter()

# --- Mock Redis Client and Decorator ---
# Since we cannot connect to a real Upstash Redis instance, we will mock the Redis logic
# using a simple in-memory dictionary for the MVP.

MOCK_REDIS_DB = {}

class MockRedisClient:
    """A simple in-memory mock for Redis operations."""
    def get(self, key: str):
        return MOCK_REDIS_DB.get(key)

    def setex(self, key: str, time: int, value: str):
        MOCK_REDIS_DB[key] = value
        # In a real scenario, we would also set the expiry time
        logger.debug(f"MockRedis: Setex {key} with value {value}. Expiry: {time}s")

    def incr(self, key: str):
        MOCK_REDIS_DB[key] = MOCK_REDIS_DB.get(key, 0) + 1
        return MOCK_REDIS_DB[key]

mock_redis = MockRedisClient()

def get_redis_client(request: Request) -> MockRedisClient:
    """Dependency to get the Redis client (mocked for this MVP)."""
    # In a real app, this would return request.app.state.redis_client
    return mock_redis

# --- Main API Endpoint ---

@router.post(
    "/find-nearest",
    response_model=FindNearestResponse,
    responses={
        400: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    }
)
async def find_nearest(
    request: Request,
    data: FindNearestRequest,
    redis_client: MockRedisClient = Depends(get_redis_client)
):
    """
    Finds the 5 nearest POIs to the provided address after security checks.
    Implements TSD FR-001, FR-002, FR-003, FR-004, FR-005, FR-009.
    """
    start_time = time.time()
    ip = get_client_ip(request)
    
    # 1. Verify Turnstile (TSD FR-002)
    if not await verify_turnstile(data.turnstile_token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="TURNSTILE_FAILED",
                detail="Human verification failed. Please try again."
            ).model_dump()
        )

    # 2. Rate limit per IP (TSD FR-003)
    rate_limit_key = f"rate_limit:{ip}"
    if redis_client.get(rate_limit_key) is not None:
        # Implements TSD Section 4.3: Error Response Format
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ErrorResponse(
                error="RATE_LIMIT_EXCEEDED",
                detail="One request per 24h allowed.",
                retry_after_seconds=settings.RATE_LIMIT_SECONDS
            ).model_dump()
        )
    
    # 3. Circuit breaker (TSD FR-009)
    mapbox_counter_key = "mapbox_monthly_counter"
    current_count = int(redis_client.get(mapbox_counter_key) or 0)
    if current_count >= settings.MAX_MAPBOX_MONTHLY:
        # Implements TSD Section 5.3: Mapbox downtime → return 503
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="MAPBOX_CIRCUIT_BREAKER",
                detail="Service temporarily unavailable: Mapbox quota exceeded for the month."
            ).model_dump()
        )

    # 4. Geocode (TSD FR-004)
    try:
        user_lat, user_lon = await geocode_address(data.address)
    except HTTPException as e:
        # Re-raise controlled exceptions from geocoding_service
        raise e
    except Exception as e:
        logger.error(f"Geocoding failed unexpectedly: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="GEOCODING_ERROR",
                detail="An unexpected error occurred during geocoding."
            ).model_dump()
        )

    # 5. Increment counters (TSD Section 4.4)
    redis_client.incr(mapbox_counter_key)
    redis_client.setex(rate_limit_key, settings.RATE_LIMIT_SECONDS, "1")
    
    # 6. Find nearest POIs (TSD FR-005)
    poi_service: POIService = request.app.state.poi_service
    nearest_results = poi_service.find_nearest_pois(user_lat, user_lon)
    
    # Store the result IDs in a cookie for the KMZ download endpoint (TSD FR-007)
    result_ids = ",".join([r.name for r in nearest_results]) # Using name as a mock ID for simplicity
    
    response_data = FindNearestResponse(
        results=nearest_results,
        user_lat=user_lat,
        user_lon=user_lon
    )
    
    end_time = time.time()
    logger.info(f"Request processed in {end_time - start_time:.3f}s. Mapbox count: {redis_client.get(mapbox_counter_key)}")

    # Implements TSD FR-006: Display Results as Cards
    # The actual display is done by the frontend, but we prepare the response.
    response = JSONResponse(content=response_data.model_dump())
    
    # Set a cookie with the result IDs for KMZ download
    response.set_cookie(key="last_result_ids", value=result_ids, max_age=3600, httponly=True, secure=True)
    
    return response

# --- KMZ Download Endpoint ---

@router.get(
    "/download-kmz",
    responses={
        503: {"model": ErrorResponse},
    }
)
async def download_kmz(request: Request):
    """
    Generates and streams a KMZ file of the last 5 results.
    Implements TSD FR-007.
    """
    # Implements TSD Section 4.3: GET /api/download-kmz (via cookie)
    result_ids_str = request.cookies.get("last_result_ids")
    
    if not result_ids_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="NO_LAST_RESULT",
                detail="No previous search result found. Please run a search first."
            ).model_dump()
        )

    # In a real scenario, we would retrieve the full POI data based on the IDs
    # stored in the cookie. For this MVP, we will mock the retrieval based on the names.
    poi_service: POIService = request.app.state.poi_service
    
    # Mock retrieval of PublicPOIResult objects based on names (not ideal, but functional for MVP)
    # In a real system, the cookie would store POI IDs, and we'd retrieve the full POI objects,
    # then convert them to PublicPOIResult for KMZ generation.
    
    # For simplicity in this MVP, we will assume the KMZ generation service can work with
    # the POI objects directly from the MasterList, and we'll just filter the MasterList.
    
    target_names = result_ids_str.split(',')
    target_pois = [p for p in poi_service.master_list if p.name in target_names]
    
    # Mock the conversion to PublicPOIResult for KMZ generation
    # This is a simplification, as the KMZ should ideally use the *calculated* distance.
    # Since we don't have the user's coordinates here, we'll use a mock distance.
    mock_results: List[PublicPOIResult] = []
    for poi in target_pois:
        mock_results.append(
            PublicPOIResult(
                name=poi.name,
                distance_km=0.0, # Mock distance
                google_maps_link=f"https://www.google.com/maps/dir/?api=1&destination={poi.lat},{poi.lon}",
                image_url=f"/static/images/{poi.images[0]}" if poi.images else "",
                lat=poi.lat,
                lon=poi.lon
            )
        )

    try:
        kmz_content = await generate_kmz(mock_results)
        
        # Implements TSD Section 4.3: application/vnd.google-earth.kmz
        return Response(
            content=kmz_content,
            media_type="application/vnd.google-earth.kmz",
            headers={
                "Content-Disposition": "attachment; filename=nearest_pois.kmz",
                # Implements TSD Section 6: KMZ generation Fallback 2
                "X-KMZ-Status": "Success"
            }
        )
    except Exception as e:
        logger.error(f"KMZ generation failed: {e}")
        # Implements TSD Section 6: KMZ generation Fallback 1
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="KMZ_DOWNLOAD_UNAVAILABLE",
                detail="Download temporarily unavailable due to generation error."
            ).model_dump()
        )
