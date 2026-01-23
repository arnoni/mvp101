from fastapi import APIRouter, Request, HTTPException, status, Depends, Response
import structlog
from typing import Optional
from urllib.parse import quote, unquote

from app.core.config import settings
from app.models.dto import (
    FindNearestRequest, 
    FindNearestResponse, 
    ErrorResponse, 
    PublicPOIResult,
)
from app.services.area_bucketer import AreaBucketer
from app.services.entitlement_service import EntitlementService, TierStatus
from app.services.policy_engine import PolicyEngine, RequestContext, PolicyVerdict
from app.services.poi_service import POIService
from app.services.quota_repository import QuotaRepository
from app.services.kmz_service import generate_kmz
from app.utils.security import verify_turnstile, get_client_ip

router = APIRouter()
logger = structlog.get_logger(__name__)

# --- Helper for Error ID ---
def get_req_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None)

# --- Dependencies ---

def get_quota_repo(request: Request) -> QuotaRepository:
    return request.app.state.quota_repo

def get_poi_service(request: Request) -> POIService:
    return request.app.state.poi_service

def get_policy_engine(quota_repo: QuotaRepository = Depends(get_quota_repo)) -> PolicyEngine:
    return PolicyEngine(quota_repo)

# --- Routes ---

@router.post("/find-nearest", response_model=FindNearestResponse)
async def find_nearest(
    request: Request,
    response: Response,
    data: FindNearestRequest,
    policy_engine: PolicyEngine = Depends(get_policy_engine),
    poi_service: POIService = Depends(get_poi_service),
    quota_repo: QuotaRepository = Depends(get_quota_repo),
):
    # 1. Build Context
    try:
        anon_id = getattr(request.state, "anon_id", "unknown_anon")
        client_ip = get_client_ip(request)

        # Entitlement Check
        paid_session_cookie = request.cookies.get("dd_paid_session")
        tier = EntitlementService.check_access(paid_session_cookie) if paid_session_cookie else TierStatus.FREE

        # Area Code
        area_code = AreaBucketer.get_area_code(data.lat, data.lon)

        context = RequestContext(
            anon_id=anon_id,
            paid_tier=tier,
            area_code=area_code,
            client_ip=client_ip,
            turnstile_token=data.turnstile_token
        )

        # 2. Policy Evaluate
        decision = await policy_engine.evaluate(context)

        # 3. Handle Decision
        if decision.verdict == PolicyVerdict.BLOCK:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=ErrorResponse(
                    error="QUOTA_EXCEEDED",
                    detail="Daily quota exceeded.",
                    retry_after_seconds=decision.retry_after,
                    error_id=get_req_id(request)
                ).model_dump()
            )

        if decision.verdict == PolicyVerdict.CHALLENGE_REQUIRED:
            if not data.turnstile_token:
                 raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=ErrorResponse(
                        error="CHALLENGE_REQUIRED",
                        detail="Human verification required.",
                        error_id=get_req_id(request)
                    ).model_dump()
                 )

            # Verify Token
            is_valid = await verify_turnstile(data.turnstile_token)
            if not is_valid:
                 raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=ErrorResponse(
                        error="INVALID_CHALLENGE",
                        detail="Verification failed. Please try again.",
                        error_id=get_req_id(request)
                    ).model_dump()
                 )

        # 4. Fetch Data (30m greedy)
        try:
            results, logs = poi_service.find_nearest_pois(data.lat, data.lon, max_results=decision.max_results)
        except Exception as e:
            logger.critical("poi_service_crashed", error=str(e), exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=ErrorResponse(
                    error="POI_SERVICE_ERROR",
                    detail="Internal Search Service request failed.",
                    error_id=get_req_id(request)
                ).model_dump()
            )

        # 5. Consume Quota
        quota_key = f"daily_read:{anon_id}"
        await quota_repo.increment(quota_key)

        # 6. Response Cookie for KMZ continuity
        if results:
            result_names = ",".join([p.name for p in results])
            safe_value = quote(result_names)
            response.set_cookie(key="last_result_ids", value=safe_value, httponly=True, max_age=3600)

        # 7. Construct Response
        resp = FindNearestResponse(
            results=results,
            user_lat=data.lat,
            user_lon=data.lon,
            # We just consumed 1 unit
            quota_remaining=max(0, decision.quota_remaining - 1),
            share_url=f"/share?lat={data.lat}&lon={data.lon}",
            debug_logs=logs if settings.ENV == "development" else None
        )
        # Log successful processing with structlog
        logger.info("search_request_processed", anon_id=anon_id, results_count=len(results))
        
        return resp

    except HTTPException:
        raise
    except Exception as e:
        err_id = get_req_id(request) or "unknown"
        logger.critical(f"unexpected_error_in_find_nearest", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="INTERNAL_LOGIC_ERROR",
                detail="An unexpected error occurred processing your request.",
                error_id=err_id
            ).model_dump()
        )


@router.get("/download-kmz")
async def download_kmz(
    request: Request,
    policy_engine: PolicyEngine = Depends(get_policy_engine),
    poi_service: POIService = Depends(get_poi_service),
    quota_repo: QuotaRepository = Depends(get_quota_repo),
):
    """
    Generate KMZ. Counts as a read.
    """
    anon_id = getattr(request.state, "anon_id", "unknown_anon")
    client_ip = get_client_ip(request)
    paid_session_cookie = request.cookies.get("dd_paid_session")
    tier = EntitlementService.check_access(paid_session_cookie) if paid_session_cookie else TierStatus.FREE
    
    # Dummy area or generic. 
    context = RequestContext(
        anon_id=anon_id,
        paid_tier=tier,
        area_code="global",
        client_ip=client_ip,
        turnstile_token=None 
    )
    
    # Policy Check
    decision = await policy_engine.evaluate(context)
    if decision.verdict == PolicyVerdict.BLOCK:
         raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ErrorResponse(
                error="QUOTA_EXCEEDED",
                detail="Daily quota exceeded.",
                retry_after_seconds=decision.retry_after,
                error_id=get_req_id(request)
            ).model_dump()
        )
    
    # Generate KMZ
    result_ids_str = request.cookies.get("last_result_ids")
    if not result_ids_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="NO_LAST_RESULT",
                detail="No previous search result found.",
                error_id=get_req_id(request)
            ).model_dump(),
        )
    
    target_names_str = unquote(result_ids_str)
    target_names = target_names_str.split(",")
    target_pois = [p for p in poi_service.master_list if p.name in target_names]
    
    mock_results: list[PublicPOIResult] = []
    for poi in target_pois:
        mock_results.append(
            PublicPOIResult(
                name=poi.name,
                distance_km=0.0,
                google_maps_link="",
                image_url="",
                lat=poi.lat,
                lon=poi.lon
            )
        )
        
    try:
        kmz_content = await generate_kmz(mock_results)
        
        # Consume Quota
        quota_key = f"daily_read:{anon_id}"
        await quota_repo.increment(quota_key)
        
        return Response(
            content=kmz_content,
            media_type="application/vnd.google-earth.kmz",
            headers={
                "Content-Disposition": "attachment; filename=nearest_pois.kmz",
                "X-KMZ-Status": "Success",
            },
        )
    except Exception as e:
        logger.error("kmz_generation_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="KMZ_GEN_FAILED",
                detail="Could not generate KMZ file.",
                error_id=get_req_id(request)
            ).model_dump(),
        )
