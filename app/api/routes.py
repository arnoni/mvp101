from fastapi import APIRouter, Request, HTTPException, status, Depends, Response
import structlog
from typing import Optional
from urllib.parse import quote, unquote
from pydantic import BaseModel, Field

from app.core.config import settings
from app.models.dto import FindNearestRequest, FindNearestResponse, ErrorResponse, StatusResponse, UserStatus
from app.services.area_bucketer import AreaBucketer
from app.services.entitlement_service import EntitlementService, TierStatus
from app.services.policy_engine import PolicyEngine, RequestContext, PolicyVerdict, PolicyDecision, run_gate
from app.services.poi_service import POIService
from app.services.quota_repository import QuotaRepository
from app.utils.security import verify_turnstile, get_client_ip, protect_mutation
from app.services.bucket_engine import BucketEngine
from app.services.precompute_repo import PrecomputeRepository
from app.services.anomaly_service import AnomalyService
from app.services.demand_service import DemandService
from app.services.report_renderer import ReportRenderer
from app.services.i18n import get_translations
from app.core.config import is_inside_da_nang_bbox

router = APIRouter()
logger = structlog.get_logger(__name__)

# --- Helper for Error ID ---
def get_req_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None)

# --- Dependencies ---

# --- Dependencies ---

def get_quota_repo(request: Request) -> QuotaRepository:
    return request.app.state.quota_repo

def get_precompute_repo(request: Request) -> PrecomputeRepository:
    return request.app.state.precompute_repo

def get_anomaly_service(request: Request) -> AnomalyService:
    return request.app.state.anomaly_service

def get_demand_service(request: Request) -> DemandService:
    return request.app.state.demand_service

def get_policy_engine(quota_repo: QuotaRepository = Depends(get_quota_repo)) -> PolicyEngine:
    return PolicyEngine(quota_repo)

# --- UGC DTO ---
class UGCReportRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=2000)
    lat: float
    lon: float
    category: Optional[str] = Field(default=None, max_length=50)
    severity: Optional[int] = None
    evidence_urls: Optional[list[str]] = None
    turnstile_token: Optional[str] = None

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
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    precompute_repo: PrecomputeRepository = Depends(get_precompute_repo),
    anomaly_service: AnomalyService = Depends(get_anomaly_service),
    demand_service: DemandService = Depends(get_demand_service),
):
    # CSRF protection for quota-consuming POST
    await protect_mutation(request)
    
    try:
        anon_id = getattr(request.state, "anon_id", "unknown_anon")
        user_id = getattr(request.state, "user_id", None)
        client_ip = get_client_ip(request)
        tier = getattr(request.state, "tier", TierStatus.FREE)
        entitlement_stale = getattr(request.state, "entitlement_stale", False)
        
        # 1. Anomaly Check
        # Identify based on user_id if present, else anon_id
        is_abusive = await anomaly_service.check_is_abusive(
            "user" if user_id else "anon", 
            user_id if user_id else anon_id
        )
        if is_abusive:
            logger.warning("abuse_detected", ip=client_ip, id=user_id or anon_id)
            # Fail silently or block? PolicyEngine handles GATE, Anomaly handles velocity.
            # We can force BLOCK via PolicyEngine or return error. 
            # Returning error "try again later" is safer.
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=ErrorResponse(error="ABUSE_LIMIT", detail="Too many variations. Please wait.").model_dump()
            )

        # 2. Bucket Engine
        from app.services.bucket_engine import BucketEngine
        cell_id = BucketEngine.get_cell_id(data.lat, data.lon)
        # Using cell_id as "area_code" logic for policy if we wanted per-cell quotas?
        # For now, stick to "area_code" from AreaBucketer for simple "global/danang" check if needed.
        area_code = AreaBucketer.get_area_code(data.lat, data.lon)

        # 3. Policy Gate
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
        
        # 4. Anomaly Record (Record this attempt AFTER gate passes or challenges?)
        # Record attempt anyway
        await anomaly_service.record_action("user" if user_id else "anon", user_id if user_id else anon_id, cell_id)
        
        # 5. Demand Record (Record interest in this cell)
        await demand_service.record_query(cell_id)

        # 6. Fetch Precomputed Data
        # Only fetch if allowed
        candidates = []
        logs = []
        if decision.verdict == PolicyVerdict.ALLOW or gate_result.admin_bypass:
            candidates = await precompute_repo.get_candidates(cell_id)
            if settings.ENV == "development":
                logs.append(f"Cell {cell_id} candidates: {len(candidates)}")
        
        # 7. Render Opaque Report
        from app.services.report_renderer import ReportRenderer
        report_lines = ReportRenderer.render(candidates, data.lat, data.lon, limit=decision.max_results)
        
        # 8. Construct Response
        remaining_after = gate_result.remaining_after
        limit = PolicyEngine.FREE_TIER_DAILY_LIMIT if tier == TierStatus.FREE else PolicyEngine.PAID_TIER_DAILY_LIMIT
        checks_today = max(0, limit - remaining_after)
        
        lang = request.cookies.get("dd_lang") or "en"
        t = get_translations(lang)
        # Determine status text
        can_search = (decision.verdict != PolicyVerdict.BLOCK or gate_result.admin_bypass)
        if not can_search:
            status_text = t.get("status_limit", "Daily limit reached")
            state = "limit"
        elif checks_today == 0:
            status_text = t.get("status_quiet", "Quiet check available")
            state = "quiet"
         # Correct "active" text logic
        else:
             status_text = t.get("status_active_many", "You’ve checked {n} places today").replace("{n}", str(checks_today))
             state = "active"

        tier_str = "pro" if tier == TierStatus.PAID else "free"
        results_state = "found" if len(report_lines) > 0 else "empty"
        if not can_search: results_state = "never"

        resp = FindNearestResponse(
            report_lines=report_lines,
            user_lat=data.lat,
            user_lon=data.lon,
            quota_remaining=remaining_after,
            share_url=f"/share?lat={data.lat}&lon={data.lon}",
            debug_logs=logs if settings.ENV == "development" else None,
            user_status=UserStatus(state=state, text=status_text),
            can_search=can_search,
            turnstile_required=False, # We don't ask for TS in response generally, client logic handles based on 402/Challenge? 
            # PolicyEngine returns CHALLENGE_REQUIRED. 
            # If Result was CHALLENGE_REQUIRED, run_gate would have thrown exception if token missing.
            # If token was present and valid, run_gate returned ALLOW.
            # So here turnstile_required is False unless next one needs it?
            # Actually frontend uses this flag to show widget.
            # If we just consumed quota, maybe we are good.
            # But let's assume False for now.
            checks_today=checks_today,
            tier=tier_str,
            results_state=results_state,
            errors=None
        )
        
        logger.info("search_request_processed", anon_id=anon_id, cell_id=cell_id, results_count=len(report_lines))
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



@router.post("/ugc/report-submit")
async def ugc_report_submit(
    request: Request,
    data: UGCReportRequest,
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    policy_engine: PolicyEngine = Depends(get_policy_engine),
):
    anon_id = getattr(request.state, "anon_id", "unknown_anon")
    user_id = getattr(request.state, "user_id", None)
    tier = getattr(request.state, "tier", TierStatus.FREE)
    entitlement_stale = getattr(request.state, "entitlement_stale", False)
    if not is_inside_da_nang_bbox(data.lat, data.lon):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(error="OUT_OF_BOUNDS", detail="Coordinates outside allowed area.").model_dump()
        )
    if data.severity is not None:
        if data.severity < 1 or data.severity > 5:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ErrorResponse(error="INVALID_SEVERITY", detail="Severity must be between 1 and 5.").model_dump()
            )
    if data.evidence_urls is not None:
        if len(data.evidence_urls) > 5:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ErrorResponse(error="TOO_MANY_EVIDENCE_URLS", detail="Maximum 5 evidence URLs allowed.").model_dump()
            )
        for u in data.evidence_urls:
            if len(u) > 500:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=ErrorResponse(error="EVIDENCE_URL_TOO_LONG", detail="Evidence URL exceeds maximum length.").model_dump()
                )
    anon_id = getattr(request.state, "anon_id", "unknown_anon")
    user_id = getattr(request.state, "user_id", None)
    tier = getattr(request.state, "tier", TierStatus.FREE)
    entitlement_stale = getattr(request.state, "entitlement_stale", False)
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
        force_turnstile_required=True,
        disallow_admin_bypass=True,
    )
    import hashlib, json, time, uuid
    from sqlalchemy import text
    redis_cli = getattr(request.app.state, "redis", None)
    if not redis_cli:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")
    def norm_text(s: str) -> str:
        return " ".join((s or "").strip().lower().split())
    title_n = norm_text(data.title)
    desc_n = norm_text(data.description)
    cat_n = norm_text(data.category or "")
    def quantize_coord(v: float, step: float = 0.0005) -> float:
        return round(round(v / step) * step, 6)
    lat_q = quantize_coord(float(data.lat))
    lon_q = quantize_coord(float(data.lon))
    geo_cell = f"{lat_q}:{lon_q}"
    content_hash = hashlib.sha256(f"{title_n}|{desc_n}|{cat_n}".encode("utf-8")).hexdigest()
    day_bucket = time.strftime("%Y%m%d", time.gmtime())
    dedup_key = hashlib.sha256(f"{anon_id}|{geo_cell}|{content_hash}|{day_bucket}".encode("utf-8")).hexdigest()
    dedup_redis_key = f"ugc:dedup:{dedup_key}"
    public_id = str(uuid.uuid4())
    claimed = await redis_cli.set(dedup_redis_key, public_id, ex=7 * 24 * 3600, nx=True)
    if not claimed:
        existing = await redis_cli.get(dedup_redis_key)
        return {"ok": True, "report_id": existing, "duplicate": True}
    db_engine = getattr(request.app.state, "db_engine", None)
    if not db_engine:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")
    UGC_INSERT_SQL = text("""
    INSERT INTO ugc_reports (
      public_id,
      reporter_anon_id,
      reporter_user_id,
      reporter_tier,
      title,
      description,
      category,
      severity,
      geom,
      status,
      content_hash,
      geo_cell
    )
    VALUES (
      :public_id::uuid,
      :reporter_anon_id,
      :reporter_user_id,
      :reporter_tier,
      :title,
      :description,
      :category,
      :severity,
      ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
      'pending'::ugc_report_status,
      :content_hash,
      :geo_cell
    )
    RETURNING id, public_id
    """)
    try:
      async with db_engine.begin() as conn:
        row = (await conn.execute(
          UGC_INSERT_SQL,
          {
            "public_id": public_id,
            "reporter_anon_id": anon_id,
            "reporter_user_id": user_id,
            "reporter_tier": "pro" if tier == TierStatus.PAID else "free",
            "title": data.title,
            "description": data.description,
            "category": data.category,
            "severity": data.severity,
            "lat": float(data.lat),
            "lon": float(data.lon),
            "content_hash": content_hash,
            "geo_cell": geo_cell,
          }
        )).first()
        public_id = str(row.public_id)
    except Exception:
      raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=ErrorResponse(error="STORAGE_UNAVAILABLE", detail="Database unavailable.").model_dump())
    if data.evidence_urls:
        try:
            await redis_cli.set(f"ugc:evidence:{public_id}", json.dumps(data.evidence_urls), ex=7 * 24 * 3600)
        except Exception:
            pass
    try:
        await redis_cli.set(dedup_redis_key, public_id, ex=7 * 24 * 3600)
    except Exception:
        pass
    return {"ok": True, "report_id": public_id, "duplicate": False}

