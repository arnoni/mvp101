from fastapi import APIRouter, Request, HTTPException, status, Depends, Response
import structlog
from typing import Optional
from urllib.parse import quote, unquote
from pydantic import BaseModel, Field

from app.core.config import settings
from app.models.dto import FindNearestRequest, FindNearestResponse, ErrorResponse, PublicPOIResultWithCoords, StatusResponse, UserStatus
from app.services.area_bucketer import AreaBucketer
from app.services.entitlement_service import EntitlementService, TierStatus
from app.services.policy_engine import PolicyEngine, RequestContext, PolicyVerdict, PolicyDecision, run_gate
from app.services.poi_service import POIService
from app.services.quota_repository import QuotaRepository
from app.services.kmz_service import generate_kmz
from app.utils.security import verify_turnstile, get_client_ip, protect_mutation
from app.services.i18n import get_translations
from app.core.config import is_inside_da_nang_bbox

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

# --- UGC DTO ---
class UGCReportRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=2000)
    lat: float
    lon: float
    category: Optional[str] = Field(default=None, max_length=50)
    evidence_urls: Optional[list[str]] = None

# --- Routes ---

@router.get("/status", response_model=StatusResponse)
async def status(
    request: Request,
    policy_engine: PolicyEngine = Depends(get_policy_engine),
    quota_repo: QuotaRepository = Depends(get_quota_repo),
):
    try:
        anon_id = getattr(request.state, "anon_id", "unknown_anon")
        user_id = getattr(request.state, "user_id", None)
        client_ip = get_client_ip(request)
        tier = getattr(request.state, "tier", TierStatus.FREE)
        entitlement_stale = getattr(request.state, "entitlement_stale", False)
        admin_hdr = request.headers.get("X-Admin-Auth")
        admin_bypass = bool(settings.ADMIN_BYPASS_TOKEN and admin_hdr and admin_hdr == settings.ADMIN_BYPASS_TOKEN)

        area_code = "global"
        context = RequestContext(
            anon_id=anon_id,
            paid_tier=tier,
            area_code=area_code,
            client_ip=client_ip,
            turnstile_token=None,
            user_id=user_id,
            entitlement_stale=entitlement_stale
        )

        decision = PolicyDecision(verdict=PolicyVerdict.ALLOW, quota_remaining=999, max_results=5) if admin_bypass else await policy_engine.evaluate(context)

        limit = PolicyEngine.FREE_TIER_DAILY_LIMIT
        if tier == TierStatus.PAID:
            limit = PolicyEngine.PAID_TIER_DAILY_LIMIT

        can_search = True
        turnstile_required = False
        if not admin_bypass:
            if decision.verdict == PolicyVerdict.BLOCK:
                can_search = False
            elif decision.verdict == PolicyVerdict.CHALLENGE_REQUIRED:
                turnstile_required = True

        checks_today = 0
        if decision.quota_remaining is not None:
            checks_today = max(0, limit - decision.quota_remaining)

        lang = request.cookies.get("dd_lang") or "en"
        t = get_translations(lang)
        if not can_search:
            status_text = t.get("status_limit", "Daily limit reached")
            state = "limit"
        elif checks_today == 0:
            status_text = t.get("status_quiet", "Quiet check available")
            state = "quiet"
        elif checks_today == 1:
            status_text = t.get("status_active_one", "You’ve checked 1 place today")
            state = "active"
        else:
            status_text = t.get("status_active_many", "You’ve checked {n} places today").replace("{n}", str(checks_today))
            state = "active"

        tier_str = "pro" if tier == TierStatus.PAID else "free"

        return StatusResponse(
            user_status=UserStatus(state=state, text=status_text),
            can_search=can_search,
            turnstile_required=turnstile_required,
            checks_today=checks_today,
            tier=tier_str
        )
    except Exception as e:
        logger.error("status_endpoint_failed", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="STATUS_FAILED",
                detail="Could not compute status."
            ).model_dump()
        )

@router.post("/find-nearest", response_model=FindNearestResponse)
async def find_nearest(
    request: Request,
    response: Response,
    data: FindNearestRequest,
    policy_engine: PolicyEngine = Depends(get_policy_engine),
    poi_service: POIService = Depends(get_poi_service),
    quota_repo: QuotaRepository = Depends(get_quota_repo),
):
    # CSRF protection for quota-consuming POST
    await protect_mutation(request)
    # 1. Build Context
    try:
        anon_id = getattr(request.state, "anon_id", "unknown_anon")
        user_id = getattr(request.state, "user_id", None)
        client_ip = get_client_ip(request)

        # Entitlement Check
        tier = getattr(request.state, "tier", TierStatus.FREE)
        entitlement_stale = getattr(request.state, "entitlement_stale", False)
        
        # Area Code
        area_code = AreaBucketer.get_area_code(data.lat, data.lon)
        gate_result = await run_gate(
            request=request,
            data_turnstile_token=data.turnstile_token,
            policy_engine=policy_engine,
            quota_repo=quota_repo,
            anon_id=anon_id,
            user_id=user_id,
            tier=tier,
            entitlement_stale=entitlement_stale,
            area_code=area_code,
        )
        decision = gate_result.decision

        # 4. Fetch Data (30m greedy, PostGIS-backed)
        try:
            results, logs = await poi_service.find_nearest_pois(data.lat, data.lon, max_results=decision.max_results)
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

        remaining_after = gate_result.remaining_after

        # 6. Response Cookie for KMZ continuity
        if results:
            result_names = ",".join([p.name for p in results])
            safe_value = quote(result_names)
            response.set_cookie(key="last_result_ids", value=safe_value, httponly=True, max_age=3600)

        # 7. Construct Response
        limit = PolicyEngine.FREE_TIER_DAILY_LIMIT
        if tier == TierStatus.PAID:
            limit = PolicyEngine.PAID_TIER_DAILY_LIMIT
        checks_today = max(0, limit - remaining_after)
        lang = request.cookies.get("dd_lang") or "en"
        t = get_translations(lang)
        if remaining_after <= 0:
            status_text = t.get("status_limit", "Daily limit reached")
            state = "limit"
        elif checks_today == 0:
            status_text = t.get("status_quiet", "Quiet check available")
            state = "quiet"
        elif checks_today == 1:
            status_text = t.get("status_active_one", "You’ve checked 1 place today")
            state = "active"
        else:
            status_text = t.get("status_active_many", "You’ve checked {n} places today").replace("{n}", str(checks_today))
            state = "active"
        turnstile_required = False
        results_state = "found" if len(results) > 0 else "empty"
        tier_str = "pro" if tier == TierStatus.PAID else "free"
        resp = FindNearestResponse(
            results=results,
            user_lat=data.lat,
            user_lon=data.lon,
            quota_remaining=remaining_after,
            share_url=f"/share?lat={data.lat}&lon={data.lon}",
            debug_logs=logs if settings.ENV == "development" else None,
            user_status=UserStatus(state=state, text=status_text),
            can_search=(decision.verdict != PolicyVerdict.BLOCK or gate_result.admin_bypass),
            turnstile_required=turnstile_required,
            checks_today=checks_today,
            tier=tier_str,
            results_state=results_state,
            errors=None
        )
        logger.info("search_request_processed", anon_id=anon_id, area_code=area_code, results_count=len(results))
        
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
    poi_service: POIService = Depends(get_poi_service),
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    policy_engine: PolicyEngine = Depends(get_policy_engine),
):
    """
    Generate KMZ. Counts as a read.
    """
    anon_id = getattr(request.state, "anon_id", "unknown_anon")
    user_id = getattr(request.state, "user_id", None)
    tier = getattr(request.state, "tier", TierStatus.FREE)
    entitlement_stale = getattr(request.state, "entitlement_stale", False)
    
    # Gate and consume quota before generating KMZ
    gate_result = await run_gate(
        request=request,
        data_turnstile_token=None,
        policy_engine=policy_engine,
        quota_repo=quota_repo,
        anon_id=anon_id,
        user_id=user_id,
        tier=tier,
        entitlement_stale=entitlement_stale,
        area_code="global",
    )
    decision = gate_result.decision
    
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
    mock_results: list[PublicPOIResultWithCoords] = await poi_service.get_pois_by_names(target_names, include_coords=True)
        
    try:
        kmz_content = await generate_kmz(mock_results)
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

@router.post("/ugc/report-submit")
async def ugc_report_submit(
    request: Request,
    data: UGCReportRequest,
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    policy_engine: PolicyEngine = Depends(get_policy_engine),
):
    # 1. CSRF protection for mutation
    await protect_mutation(request)
    # 2. Build Context
    anon_id = getattr(request.state, "anon_id", "unknown_anon")
    user_id = getattr(request.state, "user_id", None)
    tier = getattr(request.state, "tier", TierStatus.FREE)
    entitlement_stale = getattr(request.state, "entitlement_stale", False)
    area_code = AreaBucketer.get_area_code(data.lat, data.lon)
    # 3. Gate (consume quota before heavier work)
    gate_result = await run_gate(
        request=request,
        data_turnstile_token=None,
        policy_engine=policy_engine,
        quota_repo=quota_repo,
        anon_id=anon_id,
        user_id=user_id,
        tier=tier,
        entitlement_stale=entitlement_stale,
        area_code=area_code,
    )
    # 4. Deep validation
    if not is_inside_da_nang_bbox(data.lat, data.lon):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(error="OUT_OF_BOUNDS", detail="Coordinates outside allowed area.").model_dump()
        )
    # 5. Dedup check and write (Redis-backed)
    import hashlib, json, time
    redis_cli = getattr(request.app.state, "redis", None)
    if not redis_cli:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")
    normalized = f"{data.title.strip().lower()}|{area_code}|{anon_id or 'anon'}"
    rid = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    key = f"ugc:report:{rid}"
    payload = {
        "title": data.title,
        "description": data.description,
        "lat": data.lat,
        "lon": data.lon,
        "area_code": area_code,
        "category": data.category,
        "evidence_urls": data.evidence_urls or [],
        "by_user_id": user_id,
        "by_anon_id": anon_id,
        "created_at": int(time.time())
    }
    try:
        # NX -> only set if not exists, TTL 7 days
        ok = await redis_cli.set(key, json.dumps(payload), ex=7 * 24 * 3600, nx=True)
        duplicate = False if ok else True
    except Exception:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")
    # 6. Return
    return {"ok": True, "report_id": rid, "duplicate": duplicate}

