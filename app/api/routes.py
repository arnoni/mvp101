from fastapi import APIRouter, Request, HTTPException, status as http_status, Depends, Response
import structlog
from typing import Optional
from urllib.parse import quote, unquote
from pydantic import BaseModel, Field
from pydantic import ValidationError
import uuid
from sqlalchemy import insert

from app.core.config import settings
from app.models.models import FunnelEvent
from app.models.dto import ErrorResponse, StatusResponse, UserStatus
from app.schemas.search import SearchRequest, SearchResponse, SearchTarget
from app.schemas.user_reports import UserReportRequest, UserReportResponse
from app.services.search_service import SearchService, SearchDependencies
from app.services.area_bucketer import AreaBucketer
from app.services.entitlement_service import EntitlementService, TierStatus
from app.services.policy_engine import PolicyEngine, RequestContext, PolicyVerdict, PolicyDecision, run_gate
from app.services.quota_repository import QuotaRepository
from app.utils.security import verify_turnstile, get_client_ip, protect_mutation
from app.services.bucket_engine import BucketEngine
from app.services.precompute_repo import PrecomputeRepository
from app.services.demand_service import DemandService
from app.services.i18n import get_translations
from app.core.config import is_inside_app_bbox
from app.services.location_parser import (
    InvalidCoordinateRangeError,
    LocationResolutionBlockedError,
    MalformedLocationInputError,
    ParsedLocationInput,
    ShortUrlResolutionError,
    UnsupportedLocationInputError,
    LocationNotSupportedError,
    LocationParseError,
    parse_location_input,
)
from app.services.location_input_classifier import classify_location_input
from app.services.input_format_stats_service import increment_input_format_stats
from app.services.query_history_repository import QueryHistoryEvent, QueryHistoryRepository
import time

router = APIRouter()
logger = structlog.get_logger(__name__)


def _raise_location_resolution_blocked_http_exception(msg: str | None = None) -> None:
    from app.services.location_parser import _BLOCKED_RESOLUTION_MESSAGE
    raise HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "error": "LOCATION_RESOLUTION_FAILED",
            "error_code": "SHORT_URL_RESOLUTION_BLOCKED",
            "message": msg or _BLOCKED_RESOLUTION_MESSAGE
        }
    )


def _raise_location_parse_http_exception(error_code: str = "INVALID_LOCATION_INPUT") -> None:
    raise HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "error": "LOCATION_PARSE_FAILED",
            "error_code": error_code,
        }
    )

def _raise_location_not_supported_http_exception(msg: str) -> None:
    raise HTTPException(
        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "error": "LOCATION_PARSE_FAILED",
            "error_code": "LOCATION_NOT_SUPPORTED",
            "message": msg
        }
    )

# --- Helper for Error ID ---
def get_req_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None)


def tier_to_client(tier: TierStatus) -> str:
    if tier == TierStatus.SIMULATED_PAID:
        return "simulated_paid"
    if tier == TierStatus.PASS_3_DAY:
        return "3_day"
    if tier == TierStatus.PASS_1_DAY:
        return "1_day"
    return "free"


def _tier_to_funnel(tier: TierStatus) -> str:
    if tier == TierStatus.SIMULATED_PAID:
        return "simulated_paid"
    if tier == TierStatus.PASS_1_DAY:
        return "paid_1_day"
    if tier == TierStatus.PASS_3_DAY:
        return "paid_3_days"
    return "free"


async def _emit_funnel_event(request: Request, **values) -> None:
    db_engine = getattr(request.app.state, "db_engine", None)
    if not db_engine:
        return
    payload = {
        "event_source": "api",
        "event_version": 1,
        "anon_id": getattr(request.state, "anon_id", None),
        "session_id": request.cookies.get(settings.SESSION_COOKIE_NAME),
        "user_id": getattr(request.state, "user_id", None),
        "effective_tier": "free",
        "selected_language": request.cookies.get("dd_lang") or "en",
        "metadata_json": {},
    }
    payload.update(values)
    try:
        async with db_engine.begin() as conn:
            await conn.execute(insert(FunnelEvent).values(**payload))
    except Exception as exc:
        route_path = request.url.path if request.url else "unknown"
        logger.error(
            "funnel_event_emit_failed",
            route=route_path,
            event_name=payload.get("event_name"),
            user_id=payload.get("user_id"),
            session_id=payload.get("session_id"),
            anon_id=payload.get("anon_id"),
            error_class=exc.__class__.__name__,
            error=str(exc),
        )
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("telemetry_kind", "funnel_event")
                scope.set_tag("route", route_path)
                scope.set_tag("event_name", str(payload.get("event_name")))
                scope.set_extra("payload", payload)
                sentry_sdk.capture_exception(exc)
        except Exception:
            logger.error("funnel_event_sentry_capture_failed", route=route_path, event_name=payload.get("event_name"))

# --- Dependencies ---

# --- Dependencies ---

def get_quota_repo(request: Request) -> QuotaRepository:
    return request.app.state.quota_repo

def get_precompute_repo(request: Request) -> PrecomputeRepository:
    return request.app.state.precompute_repo

def get_demand_service(request: Request) -> DemandService:
    return request.app.state.demand_service

def get_query_history_repo(request: Request) -> QueryHistoryRepository:
    return request.app.state.query_history_repo

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




def _map_user_report_to_ugc(data: UserReportRequest) -> UGCReportRequest:
    category_map = {
        "active_construction": "active_construction",
        "maybe_construction": "unsure_but_suspicious",
        "construction_ended": "new_site_spotted",
    }
    title_map = {
        "active_construction": "Active construction observed",
        "maybe_construction": "Possible construction observed",
        "construction_ended": "Construction appears ended",
    }
    category = category_map.get(data.report_kind.value, "active_construction")
    fallback_title = title_map[data.report_kind.value]
    note = data.note if data.note is not None else ""
    description = note if note.strip() else fallback_title
    return UGCReportRequest(
        title=fallback_title,
        description=description,
        lat=data.lat,
        lon=data.lon,
        category=category,
        severity=3,
        evidence_urls=None,
        turnstile_token=data.turnstile_token,
    )

class ParseLocationRequest(BaseModel):
    location_input: str = Field(..., min_length=1, max_length=2048)


class ParseLocationResponse(BaseModel):
    ok: bool
    normalized: dict | None = None
    error_code: str | None = None
    message: str | None = None


class ClientFlowEventRequest(BaseModel):
    event: str = Field(..., min_length=3, max_length=128)
    flow_type: str = Field(default="research_access", min_length=2, max_length=64)
    status: str | None = Field(default=None, max_length=64)
    ui_surface: str | None = Field(default=None, max_length=64)
    step: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=400)

# --- Routes ---

@router.get("/test-email")
async def test_email(email: str = "dilldrillteam@gmail.com"):
    from app.services.email_service import EmailService
    service = EmailService()
    success = await service.send_test_email(email)
    if success:
        return {"message": f"Test email sent to {email}"}
    return {"message": "Failed to send test email", "status": 500}

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
        daily_limit = int(getattr(request.state, "daily_limit", 3) or 3)
        active_plan_code = getattr(request.state, "active_plan_code", None)
        expires_at = getattr(request.state, "expires_at", None)
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
            entitlement_stale=entitlement_stale,
            daily_limit=daily_limit,
        )

        decision = PolicyDecision(verdict=PolicyVerdict.ALLOW, quota_remaining=999, max_results=5) if admin_bypass else await policy_engine.evaluate(context)

        limit = daily_limit

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

        tier_str = tier_to_client(tier)

        return StatusResponse(
            user_status=UserStatus(state=state, text=status_text),
            can_search=can_search,
            turnstile_required=turnstile_required,
            checks_today=checks_today,
            tier=tier_str,
            active_plan_code=active_plan_code,
            daily_limit=daily_limit,
            expires_at=expires_at,
        )
    except Exception as e:
        logger.error("status_endpoint_failed", error=str(e))
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="STATUS_FAILED",
                detail="Could not compute status."
            ).model_dump()
        )

@router.post("/parse-location", response_model=ParseLocationResponse)
async def parse_location(data: ParseLocationRequest):
    try:
        parsed = parse_location_input(data.location_input)
        return ParseLocationResponse(
            ok=True,
            normalized={
                "latitude": parsed.latitude,
                "longitude": parsed.longitude,
                "display": parsed.normalized_input,
                "input_kind": parsed.input_kind,
            },
        )
    except ShortUrlResolutionError as exc:
        _raise_location_parse_http_exception("SHORT_URL_RESOLUTION_FAILED")
    except LocationResolutionBlockedError as exc:
        _raise_location_resolution_blocked_http_exception(str(exc))
    except LocationNotSupportedError as exc:
        _raise_location_not_supported_http_exception(str(exc))
    except LocationParseError as exc:
        _raise_location_parse_http_exception(exc.error_code)
    except Exception as exc:
        logger.exception(
            "parse_location_unexpected_parser_failure",
            error=str(exc),
            location_input=data.location_input[:120],
        )
        _raise_location_parse_http_exception()

@router.post("/search", response_model=SearchResponse)
async def search(
    request: Request,
    data: SearchRequest,
    policy_engine: PolicyEngine = Depends(get_policy_engine),
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    precompute_repo: PrecomputeRepository = Depends(get_precompute_repo),
    demand_service: DemandService = Depends(get_demand_service),
    query_history_repo: QueryHistoryRepository = Depends(get_query_history_repo),
):
    try:
        anon_id = getattr(request.state, "anon_id", "unknown_anon")
        user_id = getattr(request.state, "user_id", None)
        tier = getattr(request.state, "tier", TierStatus.FREE)
        entitlement_stale = getattr(request.state, "entitlement_stale", False)
        daily_limit = int(getattr(request.state, "daily_limit", 3) or 3)

        # Aggregate telemetry first so blocked/challenged attempts are still counted.
        classification = classify_location_input(data.location_input or "")

        target_mode = data.target.value if hasattr(data.target, "value") else str(data.target)
        if user_id is None:
            user_state = "anonymous"
        elif tier == TierStatus.SIMULATED_PAID:
            user_state = "simulated_paid"
        elif tier in {TierStatus.PASS_1_DAY, TierStatus.PASS_3_DAY}:
            user_state = "paid"
        elif tier == TierStatus.FREE:
            user_state = "registered"
        else:
            user_state = "unknown"

        db_engine = getattr(request.app.state, "db_engine", None)
        if db_engine:
            try:
                async with db_engine.begin() as conn:
                    await increment_input_format_stats(
                        conn,
                        target_mode=target_mode,
                        input_format=classification.input_format,
                        input_parse_status=classification.parse_status,
                        input_host=classification.input_host,
                        user_state=user_state,
                    )
            except Exception:
                logger.warning(
                    "input_format_stats_increment_failed",
                    target_mode=target_mode,
                    input_format=classification.input_format,
                    input_parse_status=classification.parse_status,
                    user_state=user_state,
                    exc_info=True,
                )

        await protect_mutation(request)
        started_at = time.perf_counter()
        if data.location_input:
            try:
                parsed_input = parse_location_input(data.location_input)
            except ShortUrlResolutionError as exc:
                _raise_location_parse_http_exception("SHORT_URL_RESOLUTION_FAILED")
            except LocationResolutionBlockedError as exc:
                _raise_location_resolution_blocked_http_exception(str(exc))
            except LocationNotSupportedError as exc:
                _raise_location_not_supported_http_exception(str(exc))
            except LocationParseError as exc:
                _raise_location_parse_http_exception(exc.error_code)
            except Exception as exc:
                logger.exception(
                    "search_route_unexpected_location_parse_failure",
                    error=str(exc),
                    has_location_input=bool(data.location_input),
                )
                _raise_location_parse_http_exception()
            data.lat = parsed_input.latitude
            data.lon = parsed_input.longitude
        else:
            parsed_input = ParsedLocationInput(
                input_kind="decimal_pair",
                original_input=f"{data.lat}, {data.lon}",
                normalized_input=f"{data.lat:.6f}, {data.lon:.6f}",
                latitude=data.lat,
                longitude=data.lon,
                resolution_method="search_lat_lon",
            )
        if not is_inside_app_bbox(data.lat, data.lon):
            if settings.ENV == "preview":
                logger.info(
                    "search_out_of_bounds_allowed_in_preview",
                    lat=data.lat,
                    lon=data.lon,
                    target=target_mode,
                )
            else:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=ErrorResponse(
                        error="OUT_OF_BOUNDS",
                        detail="Location is outside supported bounding box.",
                    ).model_dump(),
                )
        area_code = AreaBucketer.get_area_code(data.lat, data.lon)
        check_types = ["construction", "demand"] if data.target == SearchTarget.BOTH else [data.target.value]

        for check_type in check_types:
            ui_surface = "demand_level_page" if check_type == "demand" else "construction_level_page"
            await _emit_funnel_event(
                request,
                event_name="check_attempted",
                effective_tier=_tier_to_funnel(tier),
                check_type=check_type,
                ui_surface=ui_surface,
            )

        try:
            gate_result = await run_gate(
                request=request,
                data_turnstile_token=data.turnstile_token,
                policy_engine=policy_engine,
                quota_repo=quota_repo,
                anon_id=anon_id,
                user_id=user_id,
                tier=tier,
                entitlement_stale=entitlement_stale,
                daily_limit=daily_limit,
                area_code=area_code,
            )
        except HTTPException:
            for check_type in check_types:
                ui_surface = "demand_level_page" if check_type == "demand" else "construction_level_page"
                await _emit_funnel_event(
                    request,
                    event_name="check_blocked_tier",
                    effective_tier=_tier_to_funnel(tier),
                    check_type=check_type,
                    ui_surface=ui_surface,
                )
            raise

        limit = daily_limit
        checks_today = max(0, limit - gate_result.remaining_after)
        tier_str = tier_to_client(tier)

        service = SearchService(
            SearchDependencies(
                redis=getattr(request.app.state, "redis", None),
                precompute_repo=precompute_repo,
                demand_service=demand_service,
                poi_service=getattr(request.app.state, "poi_service", None),
            )
        )
        response_payload = await service.run(
            request=data,
            tier=tier_str,
            quota_remaining=gate_result.remaining_after,
            checks_today=checks_today,
        )
        demand_cell_id = BucketEngine.get_cell_id(data.lat, data.lon)
        logger.info("demand_cell_id_computed", lat=data.lat, lon=data.lon, demand_cell_id=demand_cell_id)
        try:
            demand_actor_key = str(user_id or request.cookies.get(settings.SESSION_COOKIE_NAME) or anon_id or "unknown")
            demand_incremented = await demand_service.record_query(
                demand_cell_id,
                actor_key=demand_actor_key,
                dedupe_window_seconds=3600,
            )
            if not demand_incremented:
                logger.info(
                    "demand_record_query_deduped",
                    cell_id=demand_cell_id,
                    actor_key=demand_actor_key,
                    dedupe_window_seconds=3600,
                )
        except Exception:
            logger.warning("demand_record_query_failed", cell_id=demand_cell_id, target=data.target.value, exc_info=True)
        related_query_id = await query_history_repo.log_event(
            QueryHistoryEvent(
                parsed=parsed_input,
                anon_id=anon_id,
                session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
                user_id=user_id,
                demand_cell_id=demand_cell_id,
                user_agent=request.headers.get("user-agent"),
                request_country=request.headers.get("cf-ipcountry"),
                request_city=request.headers.get("x-vercel-ip-city"),
                result_status="search_completed",
                result_count=int((1 if response_payload.construction else 0) + (1 if response_payload.demand else 0)),
                error_code=None,
                response_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )
        if related_query_id is not None:
            if response_payload.construction is not None and data.target in (SearchTarget.CONSTRUCTION, SearchTarget.BOTH):
                await _emit_funnel_event(
                    request,
                    event_name="check_completed",
                    effective_tier=_tier_to_funnel(tier),
                    check_type="construction",
                    ui_surface="construction_level_page",
                    related_query_id=related_query_id,
                )
            if response_payload.demand is not None and data.target in (SearchTarget.DEMAND, SearchTarget.BOTH) and _tier_to_funnel(tier) != "free":
                await _emit_funnel_event(
                    request,
                    event_name="check_completed",
                    effective_tier=_tier_to_funnel(tier),
                    check_type="demand",
                    ui_surface="demand_level_page",
                    related_query_id=related_query_id,
                )
        return response_payload
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "search_route_unhandled_exception",
            error=str(exc),
            description="Unhandled exception while executing /api/search route.",
        )
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="SEARCH_ROUTE_FAILED",
                detail="Search request failed due to an internal error.",
                error_id=get_req_id(request),
            ).model_dump()
        )


@router.post("/construction")
async def construction_wrapper(
    request: Request,
    data: SearchRequest,
    policy_engine: PolicyEngine = Depends(get_policy_engine),
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    precompute_repo: PrecomputeRepository = Depends(get_precompute_repo),
    demand_service: DemandService = Depends(get_demand_service),
    query_history_repo: QueryHistoryRepository = Depends(get_query_history_repo),
):
    data.target = SearchTarget.CONSTRUCTION
    payload = await search(request, data, policy_engine, quota_repo, precompute_repo, demand_service, query_history_repo)
    return payload.construction or {"message": "No construction result"}


@router.post("/demand")
async def demand_wrapper(
    request: Request,
    data: SearchRequest,
    policy_engine: PolicyEngine = Depends(get_policy_engine),
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    precompute_repo: PrecomputeRepository = Depends(get_precompute_repo),
    demand_service: DemandService = Depends(get_demand_service),
    query_history_repo: QueryHistoryRepository = Depends(get_query_history_repo),
):
    data.target = SearchTarget.DEMAND
    payload = await search(request, data, policy_engine, quota_repo, precompute_repo, demand_service, query_history_repo)
    return payload.demand or {"message": "No demand result"}


@router.post("/user-reports", response_model=UserReportResponse)
async def user_report_submit(
    request: Request,
    data: UserReportRequest,
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    policy_engine: PolicyEngine = Depends(get_policy_engine),
):
    request_id = get_req_id(request)
    try:
        note = data.note
        note_stripped = note.strip() if isinstance(note, str) else ""
        if 0 < len(note_stripped) < 10:
            error_id = request_id or str(uuid.uuid4())
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=ErrorResponse(
                    error="REPORT_DESCRIPTION_TOO_SHORT",
                    detail=f"Description is too short. Please add more detail. Error ID: {error_id}"
                ).model_dump() | {"error_id": error_id}
            )
        ugc_data = _map_user_report_to_ugc(data)
        result = await ugc_report_submit(request=request, data=ugc_data, quota_repo=quota_repo, policy_engine=policy_engine)
        return UserReportResponse(ok=result["ok"], report_id=result["report_id"], duplicate=result.get("duplicate", False))
    except ValidationError as exc:
        error_id = request_id or str(uuid.uuid4())
        logger.warning(
            "user_report_validation_failed",
            error_id=error_id,
            errors=exc.errors(),
            request_id=request_id,
        )
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=ErrorResponse(
                error="REPORT_VALIDATION_FAILED",
                detail=f"Report validation failed. Error ID: {error_id}"
            ).model_dump() | {"error_id": error_id}
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"detail": str(exc.detail)}
        if isinstance(detail, dict):
            detail.setdefault("error_id", request_id)
        raise HTTPException(status_code=exc.status_code, detail=detail)
    except Exception as exc:
        error_id = request_id or str(uuid.uuid4())
        logger.exception(
            "user_report_submit_failed",
            error_id=error_id,
            request_id=request_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorResponse(
                error="REPORT_SUBMIT_FAILED",
                detail=f"Failed to submit report. Error ID: {error_id}"
            ).model_dump() | {"error_id": error_id}
        )


@router.post("/language")
async def set_language(request: Request, response: Response):
    try:
        data = await request.json()
        lang = data.get("lang", "en")
        # Validate lang code (must be in TRANSLATIONS)
        from app.services.i18n import TRANSLATIONS
        if lang not in TRANSLATIONS:
            lang = "en"
        
        # Set cookie: dd_lang
        response.set_cookie(
            key="dd_lang",
            value=lang,
            max_age=31536000, # 1 year
            path="/",
            httponly=False,  # Allow JS access for UI sync if needed
            samesite="lax"
        )
        return {"ok": True, "lang": lang}
    except Exception as e:
        logger.error("set_language_failed", error=str(e))
        raise HTTPException(status_code=400, detail="Invalid request")


@router.post("/telemetry/client-event")
async def telemetry_client_event(request: Request, payload: ClientFlowEventRequest):
    await protect_mutation(request)
    logger.info(
        "client_flow_event",
        request_id=getattr(request.state, "request_id", None),
        anon_id=getattr(request.state, "anon_id", None),
        session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
        endpoint="/api/telemetry/client-event",
        event=payload.event,
        flow_type=payload.flow_type,
        status=payload.status,
        ui_surface=payload.ui_surface,
        step=payload.step,
        error_code=payload.error_code,
        error_message=payload.error_message,
    )
    return {"ok": True}

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
    daily_limit = int(getattr(request.state, "daily_limit", 3) or 3)
    if not is_inside_app_bbox(data.lat, data.lon):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(error="OUT_OF_BOUNDS", detail="Coordinates outside allowed area.").model_dump()
        )
    if data.severity is not None:
        if data.severity < 1 or data.severity > 5:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=ErrorResponse(error="INVALID_SEVERITY", detail="Severity must be between 1 and 5.").model_dump()
            )
    if data.evidence_urls is not None:
        if len(data.evidence_urls) > 5:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=ErrorResponse(error="TOO_MANY_EVIDENCE_URLS", detail="Maximum 5 evidence URLs allowed.").model_dump()
            )
        for u in data.evidence_urls:
            if len(u) > 500:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail=ErrorResponse(error="EVIDENCE_URL_TOO_LONG", detail="Evidence URL exceeds maximum length.").model_dump()
                )
    anon_id = getattr(request.state, "anon_id", "unknown_anon")
    user_id = getattr(request.state, "user_id", None)
    tier = getattr(request.state, "tier", TierStatus.FREE)
    entitlement_stale = getattr(request.state, "entitlement_stale", False)
    daily_limit = int(getattr(request.state, "daily_limit", 3) or 3)
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
        daily_limit=daily_limit,
        area_code=area_code,
        force_turnstile_required=True,
        disallow_admin_bypass=True,
    )
    import hashlib, json, time, uuid
    from sqlalchemy import text
    redis_cli = getattr(request.app.state, "redis", None)
    if not redis_cli:
        raise HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")
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
        raise HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")
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
            "reporter_tier": tier_to_client(tier),
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
    except Exception as exc:
      logger.exception(
          "ugc_report_db_insert_failed",
          error=str(exc),
          description="Failed to insert UGC report row into database.",
      )
      raise HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail=ErrorResponse(error="STORAGE_UNAVAILABLE", detail="Database unavailable.").model_dump())
    if data.evidence_urls:
        try:
            await redis_cli.set(f"ugc:evidence:{public_id}", json.dumps(data.evidence_urls), ex=7 * 24 * 3600)
        except Exception as exc:
            logger.warning(
                "ugc_report_evidence_cache_write_failed",
                error=str(exc),
                report_id=public_id,
                description="Failed to cache UGC evidence URLs in Redis.",
            )
    try:
        await redis_cli.set(dedup_redis_key, public_id, ex=7 * 24 * 3600)
    except Exception as exc:
        logger.warning(
            "ugc_report_dedup_cache_write_failed",
            error=str(exc),
            report_id=public_id,
            dedup_key=dedup_redis_key,
            description="Failed to persist UGC deduplication key in Redis.",
        )
    return {"ok": True, "report_id": public_id, "duplicate": False}
