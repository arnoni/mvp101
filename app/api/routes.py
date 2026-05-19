from fastapi import APIRouter, Request, HTTPException, status as http_status, Depends, Response
from fastapi.responses import JSONResponse
import sentry_sdk
import structlog
from typing import Optional
from urllib.parse import quote, unquote
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError
import uuid
from sqlalchemy import insert, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.keys import KeyBuilder

from app.core.config import settings
from app.models.models import FunnelEvent
from app.models.dto import ErrorResponse, StatusResponse, UserStatus
from app.schemas.search import QuotaMeta, SearchRequest, SearchResponse, SearchTarget
from app.schemas.user_reports import (
    REPORT_TYPE_TO_CATEGORY,
    REPORT_TYPE_TO_SEVERITY,
    ReportType,
    UserReportRequest,
    UserReportResponse,
)
from app.services.search_service import SearchService, SearchDependencies
from app.services.area_bucketer import AreaBucketer
from app.services.analytics import capture, posthog
from app.services.entitlement_service import EntitlementService, TierStatus
from app.services.policy_engine import GateResult, PolicyEngine, RequestContext, PolicyVerdict, PolicyDecision, run_gate
from app.services.quota_repository import QuotaRepository
from app.services.quota_service import (
    QuotaConcurrencyError,
    compute_construction_fingerprint,
    consume_construction_credit,
    get_or_initialize_remaining_quota,
    has_construction_query,
)
from app.utils.security import verify_turnstile, verify_turnstile_dependency, get_client_ip, protect_mutation
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
    parse_location_input_async,
)
from app.services.location_input_classifier import classify_location_input
from app.services.input_format_stats_service import increment_input_format_stats
from app.services.query_history_repository import QueryHistoryEvent, QueryHistoryRepository
import time
import os
import hashlib
import json
from datetime import datetime, timezone, timedelta

router = APIRouter()
# TODO(Vercel): Vercel captures stdout/console.log. structlog outputs to stderr by default.
# Ensure the deployment config routes stderr to Vercel's log drain, or configure
# structlog to write JSON to stdout for unified log aggregation.
logger = structlog.get_logger(__name__)
USER_REPORT_DAILY_SUCCESS_LIMIT = int(os.getenv("USER_REPORT_DAILY_SUCCESS_LIMIT", "3"))
UGC_DEDUP_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_CONSTRUCTION_RADIUS_M = 50
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
  geo_cell,
  day_bucket
)
VALUES (
  CAST(:public_id AS uuid),
  :reporter_anon_id,
  :reporter_user_id,
  :reporter_tier,
  :title,
  :description,
  :category,
  :severity,
  ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
  'pending'::text::ugc_report_status,
  :content_hash,
  :geo_cell,
  :day_bucket
)
RETURNING id, public_id, created_at
""")
UGC_FIND_DUPLICATE_SQL = text("""
SELECT public_id
FROM ugc_reports
WHERE content_hash = :content_hash
  AND geo_cell = :geo_cell
  AND day_bucket = :day_bucket
ORDER BY created_at ASC
LIMIT 1
""")

def _seconds_until_next_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date()
    midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc)
    return max(1, int((midnight - now).total_seconds()))


def _search_error_context(
    request: Request,
    *,
    limiter: str | None = None,
    tier: TierStatus | None = None,
    quota_remaining: int | None = None,
    quota_limit: int | None = None,
    turnstile_present: bool = False,
    turnstile_valid: bool | None = None,
) -> dict:
    return {
        "limiter": limiter,
        "tier": tier_to_client(tier or getattr(request.state, "tier", TierStatus.FREE)),
        "quota_remaining": quota_remaining,
        "quota_limit": quota_limit,
        "turnstile_present": turnstile_present,
        "turnstile_valid": turnstile_valid,
        "request_id": get_req_id(request),
    }


def _structured_search_error_response(
    status_code: int,
    error: str,
    message: str,
    *,
    retry_after_seconds: int | None = None,
    remaining: int | None = None,
    limit: int | None = None,
    scope: str | None = None,
) -> JSONResponse:
    payload = {"error": error, "message": message}
    if retry_after_seconds is not None:
        payload["retry_after_seconds"] = int(retry_after_seconds)
    if remaining is not None:
        payload["remaining"] = int(remaining)
    if limit is not None:
        payload["limit"] = int(limit)
    if scope is not None:
        payload["scope"] = scope
    return JSONResponse(status_code=status_code, content=payload)


def _search_http_exception_response(
    request: Request,
    exc: HTTPException,
    *,
    tier: TierStatus,
    quota_limit: int,
    turnstile_present: bool,
    turnstile_valid: bool | None,
) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {}
    error = detail.get("error")
    remaining = detail.get("quota_remaining")
    retry_after_seconds = detail.get("retry_after_seconds")
    if exc.status_code == http_status.HTTP_429_TOO_MANY_REQUESTS:
        code = "FREE_DAILY_QUOTA_EXCEEDED" if error in {None, "QUOTA_EXCEEDED"} else error
        retry_after_seconds = retry_after_seconds or _seconds_until_next_utc_midnight()
        logger.warning(
            "search_request_rejected",
            error=code,
            status_code=429,
            threshold_value=quota_limit,
            current_counter=max(0, quota_limit - int(remaining or 0)),
            **_search_error_context(
                request,
                limiter="anonymous",
                tier=tier,
                quota_remaining=int(remaining or 0),
                quota_limit=quota_limit,
                turnstile_present=turnstile_present,
                turnstile_valid=turnstile_valid,
            ),
        )
        return _structured_search_error_response(
            http_status.HTTP_429_TOO_MANY_REQUESTS,
            code,
            "You've used today's free checks. Try again tomorrow or join research access.",
            retry_after_seconds=retry_after_seconds,
            remaining=int(remaining or 0),
            limit=quota_limit,
            scope="anonymous",
        )
    if exc.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE:
        logger.warning(
            "search_request_rejected",
            error="SEARCH_TEMPORARILY_THROTTLED",
            status_code=503,
            **_search_error_context(
                request,
                limiter="server",
                tier=tier,
                quota_remaining=remaining,
                quota_limit=quota_limit,
                turnstile_present=turnstile_present,
                turnstile_valid=turnstile_valid,
            ),
        )
        return _structured_search_error_response(
            http_status.HTTP_503_SERVICE_UNAVAILABLE,
            "SEARCH_TEMPORARILY_THROTTLED",
            "Service temporarily busy. Please try again in a moment.",
            retry_after_seconds=int(retry_after_seconds or 30),
        )
    if exc.status_code == http_status.HTTP_403_FORBIDDEN and error in {"TURNSTILE_REQUIRED", "TURNSTILE_INVALID", "CHALLENGE_REQUIRED"}:
        code = "TURNSTILE_REQUIRED" if error in {"TURNSTILE_REQUIRED", "CHALLENGE_REQUIRED"} else "TURNSTILE_INVALID"
        logger.warning(
            "search_request_rejected",
            error=code,
            status_code=403,
            **_search_error_context(
                request,
                limiter="turnstile",
                tier=tier,
                quota_remaining=remaining,
                quota_limit=quota_limit,
                turnstile_present=turnstile_present,
                turnstile_valid=turnstile_valid,
            ),
        )
        return _structured_search_error_response(
            http_status.HTTP_403_FORBIDDEN,
            code,
            "Verification required." if code == "TURNSTILE_REQUIRED" else "Verification failed.",
        )
    raise exc

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


async def _emit_funnel_event(request: Request, **values) -> bool:
    db_engine = getattr(request.app.state, "db_engine", None)
    if not db_engine:
        route_path = request.url.path if request.url else "unknown"
        metadata = values.get("metadata_json") or {}
        logger.warning(
            "funnel_event_dropped_no_db_engine",
            route=route_path,
            event=values.get("event_name"),
            user_id=getattr(request.state, "user_id", None),
            session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
            anon_id=getattr(request.state, "anon_id", None),
            error_type="NoDatabaseEngine",
            error_detail="db_engine is not configured",
            attempt_id=metadata.get("attempt_id") or getattr(request.state, "request_id", None),
            target=metadata.get("target"),
        )
        sentry_sdk.capture_message("Funnel event dropped: no db_engine", level="warning")
        return False
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
        return True
    except Exception as exc:
        route_path = request.url.path if request.url else "unknown"
        metadata = payload.get("metadata_json") or {}
        logger.error(
            "funnel_event_insert_failed",
            route=route_path,
            event=payload.get("event_name"),
            user_id=payload.get("user_id"),
            session_id=payload.get("session_id"),
            anon_id=payload.get("anon_id"),
            error_type=type(exc).__name__,
            error_detail=str(exc),
            attempt_id=metadata.get("attempt_id") or getattr(request.state, "request_id", None),
            target=metadata.get("target"),
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
        return False

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
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=2000)
    lat: float
    lon: float
    report_type: ReportType
    evidence_urls: Optional[list[str]] = None
    turnstile_token: Optional[str] = None

    @property
    def category(self) -> str:
        return REPORT_TYPE_TO_CATEGORY[self.report_type]

    @property
    def severity(self) -> int:
        return REPORT_TYPE_TO_SEVERITY[self.report_type]




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
        report_type=data.report_type,
        evidence_urls=None,
        turnstile_token=data.cf_turnstile_token,
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
    surface: str | None = Field(default=None, max_length=64)
    modal_name: str | None = Field(default=None, max_length=64)
    action: str | None = Field(default=None, max_length=64)
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
        anon_id = getattr(request.state, "anon_id", None) or "unknown_anon"
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
async def parse_location(request: Request, data: ParseLocationRequest):
    try:
        parsed = await parse_location_input_async(
            data.location_input,
            redis_client=getattr(request.app.state, "redis", None),
        )
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
        if not data.location_input and (data.lat is None or data.lon is None):
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail={
                    "status": "missing_location",
                    "message": "Enter coordinates or a Google Maps URL to generate a report.",
                },
            )
        anon_id = getattr(request.state, "anon_id", None) or "unknown_anon"
        user_id = getattr(request.state, "user_id", None)
        tier = getattr(request.state, "tier", TierStatus.FREE)
        entitlement_stale = getattr(request.state, "entitlement_stale", False)
        daily_limit = int(getattr(request.state, "daily_limit", 3) or 3)
        turnstile_token = (data.turnstile_token or "").strip()
        turnstile_present = bool(turnstile_token)
        if not isinstance(data.target, SearchTarget):
            logger.warning(
                "invalid_search_target",
                user_id=str(user_id),
                raw_target=data.target if hasattr(data, "target") else "unparseable",
                reason="validation_error",
            )

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
        if tier == TierStatus.FREE and not turnstile_present:
            logger.warning(
                "search_request_rejected",
                error="TURNSTILE_REQUIRED",
                status_code=403,
                **_search_error_context(
                    request,
                    limiter="turnstile",
                    tier=tier,
                    quota_remaining=None,
                    quota_limit=daily_limit,
                    turnstile_present=False,
                    turnstile_valid=False,
                ),
            )
            return _structured_search_error_response(
                http_status.HTTP_403_FORBIDDEN,
                "TURNSTILE_REQUIRED",
                "Verification required.",
            )
        if data.location_input:
            try:
                parsed_input = await parse_location_input_async(
                    data.location_input,
                    redis_client=getattr(request.app.state, "redis", None),
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
        query_fingerprint = compute_construction_fingerprint(
            data.lat, data.lon, radius_m=DEFAULT_CONSTRUCTION_RADIUS_M
        )

        if user_id is not None and data.target == SearchTarget.DEMAND:
            if db_engine is None:
                logger.warning(
                    "demand_prior_construction_check_skipped",
                    reason="db_engine_missing",
                    user_id=str(user_id),
                )
            else:
                async with AsyncSession(db_engine) as quota_db:
                    demand_has_prior = await has_construction_query(
                        db=quota_db,
                        user_id=uuid.UUID(str(user_id)),
                        query_fingerprint=query_fingerprint,
                    )
                if not demand_has_prior:
                    logger.warning(
                        "demand_without_prior_construction",
                        user_id=str(user_id),
                        query_fingerprint=query_fingerprint,
                        reason="no_prior_construction_query",
                    )
                    capture(
                        str(user_id),
                        "demand_without_prior_construction",
                        {
                            "query_fingerprint": query_fingerprint,
                            "effective_tier": tier_to_client(tier),
                        },
                    )
                    return JSONResponse(
                        status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content={
                            "error": "no_prior_construction_query",
                            "message": "Request a construction report for this location first.",
                        },
                    )

        for check_type in check_types:
            ui_surface = "demand_level_page" if check_type == "demand" else "construction_level_page"
            funnel_result = await _emit_funnel_event(
                request,
                event_name="check_attempted",
                effective_tier=_tier_to_funnel(tier),
                check_type=check_type,
                ui_surface=ui_surface,
            )
            if not funnel_result:
                logger.warning(
                    "funnel_event_failed",
                    extra={
                        "event": "check_attempted",
                        "attempt_id": getattr(request.state, "request_id", None),
                        "error_code": None,
                    },
                )
            if user_id is not None:
                capture(str(user_id), "check_attempted", {"tier": _tier_to_funnel(tier)})

        paid_quota_tiers = (
            TierStatus.SIMULATED_PAID,
            TierStatus.PASS_1_DAY,
            TierStatus.PASS_3_DAY,
        )
        is_authenticated_tier = user_id is not None and tier in paid_quota_tiers
        is_authenticated_construction_quota = (
            is_authenticated_tier
            and data.target in (SearchTarget.CONSTRUCTION, SearchTarget.BOTH)
        )
        identity_kind = "paid" if is_authenticated_tier else "anon"

        # Turnstile required: token enforced via run_gate()
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
                force_turnstile_required=(tier == TierStatus.FREE),
                consume_quota=False,
            )
        except HTTPException as exc:
            exc_detail = exc.detail if isinstance(exc.detail, dict) else {}
            is_redis_quota_block = (
                exc.status_code == http_status.HTTP_429_TOO_MANY_REQUESTS
                and exc_detail.get("error") == "FREE_DAILY_QUOTA_EXCEEDED"
            )
            # For authenticated construction users, DB quota via
            # consume_construction_credit() is authoritative. Redis pre-gate
            # quota can diverge when carried-forward credits reduced the DB
            # initial remaining quota below the plan daily limit. Only suppress
            # the Redis quota block; Turnstile and other HTTPException gates
            # keep the existing early return path.
            if is_authenticated_construction_quota and is_redis_quota_block:
                quota_remaining = int(exc_detail.get("quota_remaining") or 0)
                gate_result = GateResult(
                    decision=PolicyDecision(
                        verdict=PolicyVerdict.BLOCK,
                        quota_remaining=quota_remaining,
                        max_results=PolicyEngine.PAID_TIER_RESULTS,
                        retry_after=exc_detail.get("retry_after_seconds"),
                    ),
                    remaining_after=quota_remaining,
                    admin_bypass=False,
                    quota_key=PolicyEngine.get_quota_key(user_id, anon_id, tier, entitlement_stale),
                    quota_limit=daily_limit,
                )
            else:
                for check_type in check_types:
                    ui_surface = "demand_level_page" if check_type == "demand" else "construction_level_page"
                    funnel_result = await _emit_funnel_event(
                        request,
                        event_name="check_blocked_tier",
                        effective_tier=_tier_to_funnel(tier),
                        check_type=check_type,
                        ui_surface=ui_surface,
                    )
                    if not funnel_result:
                        logger.warning(
                            "funnel_event_failed",
                            extra={
                                "event": "check_blocked_tier",
                                "attempt_id": getattr(request.state, "request_id", None),
                                "error_code": getattr(exc, "error_code", None),
                            },
                        )
                    if user_id is not None:
                        capture(str(user_id), "check_blocked_tier", {"tier": _tier_to_funnel(tier)})
                return _search_http_exception_response(
                    request,
                    exc,
                    tier=tier,
                    quota_limit=daily_limit,
                    turnstile_present=turnstile_present,
                    turnstile_valid=(False if exc.status_code == http_status.HTTP_403_FORBIDDEN else None),
                )

        logger.info(
            "quota_gate_source",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "user_id": str(user_id),
                "identity_kind": identity_kind,
                "effective_tier": tier.value,
                "redis_quota_remaining": gate_result.remaining_after,
                "quota_authority": "db" if is_authenticated_construction_quota else "redis",
                "redis_block_suppressed": (
                    gate_result.decision.verdict == PolicyVerdict.BLOCK
                    and is_authenticated_construction_quota
                ),
            },
        )

        if getattr(gate_result, "admin_bypass", False):
            logger.info(
                "admin_bypass_quota_skip",
                user_id=str(user_id),
                anon_id=anon_id,
                reason="admin_bypass",
            )

        service_quota_remaining = (
            daily_limit if is_authenticated_construction_quota else gate_result.remaining_after
        )
        limit = daily_limit
        checks_today = max(0, limit - service_quota_remaining)
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
            quota_remaining=service_quota_remaining,
            checks_today=checks_today,
            user_id=str(user_id) if user_id is not None else None,
            anon_id=anon_id,
            session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
            attempt_id=getattr(request.state, "request_id", None),
            locale=request.cookies.get("dd_lang") or "en",
        )
        if response_payload.message_code == "IN_FLIGHT":
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "SEARCH_IN_FLIGHT",
                    "message": "A search for this location is already running. Please wait.",
                },
            )

        should_consume_redis_quota = (
            user_id is None and data.target in (SearchTarget.CONSTRUCTION, SearchTarget.BOTH)
        )
        if (
            should_consume_redis_quota
            and not getattr(gate_result, "admin_bypass", False)
            and getattr(gate_result, "quota_key", None)
        ):
            raw_idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()[:128]
            idempotency_key = KeyBuilder.quota_idempotency(gate_result.quota_key, raw_idempotency_key) if raw_idempotency_key else None
            try:
                allowed, remaining_after = await quota_repo.check_and_consume(
                    gate_result.quota_key,
                    getattr(gate_result, "quota_limit", None) or daily_limit,
                    idempotency_key=idempotency_key,
                    redis_op="quota_increment",
                )
            except RuntimeError as exc:
                sentry_sdk.capture_exception(exc)
                logger.warning(
                    "search_request_rejected",
                    error="SEARCH_TEMPORARILY_THROTTLED",
                    status_code=503,
                    redis_op="quota_increment",
                    **_search_error_context(
                        request,
                        limiter="server",
                        tier=tier,
                        quota_remaining=gate_result.remaining_after,
                        quota_limit=getattr(gate_result, "quota_limit", None) or daily_limit,
                        turnstile_present=turnstile_present,
                        turnstile_valid=True,
                    ),
                )
                return _structured_search_error_response(
                    http_status.HTTP_503_SERVICE_UNAVAILABLE,
                    "SEARCH_TEMPORARILY_THROTTLED",
                    "Service temporarily busy. Please try again in a moment.",
                    retry_after_seconds=30,
                )
            if not allowed:
                sentry_sdk.capture_message(
                    f"Anonymous quota exhausted: anon_id={anon_id}, tier={tier}",
                    level="warning",
                )
                capture(
                    str(anon_id),
                    "anonymous_quota_exhausted",
                    {
                        "effective_tier": tier_to_client(tier),
                        "query_fingerprint": query_fingerprint,
                        "remaining_quota": 0,
                    },
                )
                logger.warning(
                    "search_request_rejected",
                    error="FREE_DAILY_QUOTA_EXCEEDED",
                    status_code=429,
                    threshold_value=getattr(gate_result, "quota_limit", None) or daily_limit,
                    current_counter=getattr(gate_result, "quota_limit", None) or daily_limit,
                    redis_op="quota_increment",
                    **_search_error_context(
                        request,
                        limiter="anonymous",
                        tier=tier,
                        quota_remaining=0,
                        quota_limit=getattr(gate_result, "quota_limit", None) or daily_limit,
                        turnstile_present=turnstile_present,
                        turnstile_valid=True,
                    ),
                )
                return _structured_search_error_response(
                    http_status.HTTP_429_TOO_MANY_REQUESTS,
                    "FREE_DAILY_QUOTA_EXCEEDED",
                    "You've used today's free checks. Try again tomorrow or join research access.",
                    retry_after_seconds=_seconds_until_next_utc_midnight(),
                    remaining=0,
                    limit=getattr(gate_result, "quota_limit", None) or daily_limit,
                    scope="anonymous",
                )
            response_payload.quota_remaining = remaining_after
            response_payload.checks_today = max(0, (getattr(gate_result, "quota_limit", None) or daily_limit) - remaining_after)
            if user_id is None:
                logger.info(
                    "quota_decision",
                    identity_kind="anon",
                    anon_id=anon_id,
                    report_type=data.target.value,
                    consumed=True,
                    remaining_quota=remaining_after,
                    reason="redis_only_anon_quota",
                )
                capture(
                    str(anon_id),
                    "anonymous_quota_credit_consumed",
                    {
                        "effective_tier": tier_to_client(tier),
                        "remaining_quota": remaining_after,
                        "report_type": data.target.value,
                        "query_fingerprint": query_fingerprint,
                    },
                )

        if user_id is not None and db_engine is not None:
            user_uuid = uuid.UUID(str(user_id))
            if data.target in (SearchTarget.CONSTRUCTION, SearchTarget.BOTH):
                try:
                    async with AsyncSession(db_engine) as quota_db:
                        quota_result = await consume_construction_credit(
                            db=quota_db,
                            user_id=user_uuid,
                            daily_limit=daily_limit,
                            query_fingerprint=query_fingerprint,
                        )
                except QuotaConcurrencyError:
                    logger.warning(
                        "quota_concurrency_conflict",
                        user_id=str(user_uuid),
                        query_fingerprint=query_fingerprint,
                        reason="concurrent_quota_write",
                    )
                    sentry_sdk.capture_message(
                        f"Quota concurrency conflict for user {user_uuid} on fingerprint {query_fingerprint}",
                        level="warning",
                    )
                    return _structured_search_error_response(
                        http_status.HTTP_429_TOO_MANY_REQUESTS,
                        "quota_concurrency_conflict",
                        "Quota was consumed by another request. Please retry.",
                    )
                if quota_result.reason == "insufficient_quota":
                    logger.warning(
                        "quota_exhausted",
                        user_id=str(user_uuid),
                        query_fingerprint=query_fingerprint,
                        reason="insufficient_quota",
                    )
                    capture(
                        str(user_uuid),
                        "quota_exhausted",
                        {
                            "effective_tier": tier_to_client(tier),
                            "query_fingerprint": query_fingerprint,
                            "remaining_quota": 0,
                        },
                    )
                    return JSONResponse(
                        status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                        content={
                            "error": "quota_exceeded",
                            "quota": QuotaMeta(
                                consumed=False,
                                remaining=0,
                                effective_tier=tier_to_client(tier),
                                reason="insufficient_quota",
                            ).model_dump(),
                        },
                    )
                response_payload.quota_remaining = quota_result.remaining_quota
                response_payload.checks_today = max(0, daily_limit - quota_result.remaining_quota)
                logger.info(
                    "quota_consumed",
                    user_id=str(user_uuid),
                    anon_id=anon_id,
                    remaining_before=gate_result.remaining_after,
                    remaining_after=quota_result.remaining_quota,
                    checks_today_before=checks_today,
                    checks_today_after=response_payload.checks_today,
                    consumed=quota_result.consumed,
                    reason=quota_result.reason,
                )
                response_payload.quota = QuotaMeta(
                    consumed=quota_result.consumed,
                    remaining=quota_result.remaining_quota,
                    effective_tier=tier_to_client(tier),
                    reason=quota_result.reason,
                )
                logger.info(
                    "quota_decision",
                    user_id=str(user_uuid),
                    effective_tier=tier_to_client(tier),
                    report_type="construction",
                    query_fingerprint=query_fingerprint,
                    consumed=quota_result.consumed,
                    remaining_quota=quota_result.remaining_quota,
                    reason=quota_result.reason,
                )
                if quota_result.consumed:
                    capture(
                        str(user_uuid),
                        "quota_credit_consumed",
                        {
                            "effective_tier": tier_to_client(tier),
                            "remaining_quota": quota_result.remaining_quota,
                            "report_type": "construction",
                            "query_fingerprint": query_fingerprint,
                            "surface": request.headers.get("X-DD-Surface") or "construction_level_page",
                        },
                    )
                elif quota_result.reason == "duplicate_construction_query_no_charge":
                    capture(
                        str(user_uuid),
                        "quota_duplicate_detected",
                        {
                            "effective_tier": tier_to_client(tier),
                            "remaining_quota": quota_result.remaining_quota,
                            "report_type": "construction",
                            "query_fingerprint": query_fingerprint,
                        },
                    )
            elif data.target == SearchTarget.DEMAND:
                async with AsyncSession(db_engine) as quota_db:
                    current_remaining = await get_or_initialize_remaining_quota(
                        db=quota_db, user_id=user_uuid, daily_limit=daily_limit
                    )
                response_payload.quota_remaining = current_remaining
                response_payload.checks_today = max(0, daily_limit - current_remaining)
                response_payload.quota = QuotaMeta(
                    consumed=False,
                    remaining=current_remaining,
                    effective_tier=tier_to_client(tier),
                    reason="demand_report_no_charge",
                )
                logger.info(
                    "quota_decision",
                    user_id=str(user_uuid),
                    effective_tier=tier_to_client(tier),
                    report_type="demand",
                    consumed=False,
                    remaining_quota=current_remaining,
                    reason="demand_report_no_charge",
                )
                capture(
                    str(user_uuid),
                    "demand_report_requested_no_quota_charge",
                    {
                        "effective_tier": tier_to_client(tier),
                        "remaining_quota": current_remaining,
                        "linked_construction_query_fingerprint": query_fingerprint,
                    },
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
                funnel_recorded = await _emit_funnel_event(
                    request,
                    event_name="check_completed",
                    effective_tier=_tier_to_funnel(tier),
                    check_type="construction",
                    ui_surface="construction_level_page",
                    related_query_id=related_query_id,
                    metadata_json={"target": target_mode, "cell_id": demand_cell_id},
                )
                if not funnel_recorded:
                    logger.warning(
                        "funnel_event_failed",
                        extra={
                            "event": "check_completed",
                            "attempt_id": getattr(request.state, "request_id", None),
                            "error_code": None,
                        },
                    )
                if funnel_recorded:
                    capture(
                        user_id=str(related_query_id or "unknown"),
                        event="funnel_event_recorded",
                        properties={
                            "event_type": "check_completed",
                            "target": target_mode,
                            "tier": tier_to_client(tier),
                            "cell_id": demand_cell_id,
                        },
                    )
                if user_id is not None:
                    capture(str(user_id), "check_completed", {"tier": _tier_to_funnel(tier)})
            if response_payload.demand is not None and data.target in (SearchTarget.DEMAND, SearchTarget.BOTH) and _tier_to_funnel(tier) != "free":
                funnel_recorded = await _emit_funnel_event(
                    request,
                    event_name="check_completed",
                    effective_tier=_tier_to_funnel(tier),
                    check_type="demand",
                    ui_surface="demand_level_page",
                    related_query_id=related_query_id,
                    metadata_json={"target": target_mode, "cell_id": demand_cell_id},
                )
                if not funnel_recorded:
                    logger.warning(
                        "funnel_event_failed",
                        extra={
                            "event": "check_completed",
                            "attempt_id": getattr(request.state, "request_id", None),
                            "error_code": None,
                        },
                    )
                if funnel_recorded:
                    capture(
                        user_id=str(related_query_id or "unknown"),
                        event="funnel_event_recorded",
                        properties={
                            "event_type": "check_completed",
                            "target": target_mode,
                            "tier": tier_to_client(tier),
                            "cell_id": demand_cell_id,
                        },
                    )
                if user_id is not None:
                    capture(str(user_id), "check_completed", {"tier": _tier_to_funnel(tier)})
        return response_payload
    except HTTPException:
        raise
    except Exception as exc:
        sentry_sdk.capture_exception(exc)
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

def validate_report_location(lat: float, lon: float) -> str | None:
    if not (-90 <= lat <= 90):
        return "latitude_out_of_range"
    if not (-180 <= lon <= 180):
        return "longitude_out_of_range"
    if lat == 0.0 and lon == 0.0:
        return "null_island"
    return None


def _hash_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _normalize_ugc_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _normalize_ugc_evidence_urls(urls: list[str] | None) -> list[str]:
    return sorted(_normalize_ugc_text(url) for url in (urls or []) if _normalize_ugc_text(url))


def _quantize_ugc_coord(value: float, step: float = 0.0005) -> float:
    return round(round(value / step) * step, 6)


def generate_ugc_dedup_fields(data: UGCReportRequest) -> tuple[str, str, str]:
    title_n = _normalize_ugc_text(data.title)
    desc_n = _normalize_ugc_text(data.description)
    cat_n = _normalize_ugc_text(data.category)
    evidence_n = json.dumps(_normalize_ugc_evidence_urls(data.evidence_urls), separators=(",", ":"))
    content_hash = hashlib.sha256(f"{title_n}|{desc_n}|{cat_n}|{evidence_n}".encode("utf-8")).hexdigest()
    lat_q = _quantize_ugc_coord(float(data.lat))
    lon_q = _quantize_ugc_coord(float(data.lon))
    geo_cell = f"{lat_q}:{lon_q}"
    day_bucket = time.strftime("%Y-%m-%d", time.gmtime())
    return content_hash, geo_cell, day_bucket


def generate_ugc_dedup_key(content_hash: str, geo_cell: str, day_bucket: str) -> str:
    return KeyBuilder.ugc_dedup(content_hash, geo_cell, day_bucket)


def _is_ugc_dedup_integrity_error(exc: IntegrityError) -> bool:
    if "ugc_reports_dedup_unique" in str(exc):
        return True
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    constraint_name = getattr(getattr(orig, "diag", None), "constraint_name", None)
    return sqlstate == "23505" and constraint_name == "ugc_reports_dedup_unique"


def _user_report_scope(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None) or getattr(request.state, "identity_id", None)
    anon_id = getattr(request.state, "anon_id", None)
    if user_id:
        return f"user:{user_id}"
    if anon_id:
        return f"anon:{anon_id}"
    return f"ip:{get_client_ip(request)}"


def _seconds_until_utc_midnight() -> int:
    return max(1, 86400 - (int(time.time()) % 86400))


def _flush_posthog() -> None:
    if posthog:
        posthog.flush()


async def _check_user_report_attempt_rate_limit(request: Request, redis_cli, *, limit: int = 10, window_seconds: int = 60) -> None:
    scope = _user_report_scope(request)
    key = f"rate_limit:user_reports:attempts:{scope}:{int(time.time() // window_seconds)}"
    used = await redis_cli.incr(key)
    if used == 1:
        await redis_cli.expire(key, window_seconds)
    decision = "allow" if used <= limit else "block"
    logger.info(
        "user_report_attempt_rate_limit_checked",
        request_id=get_req_id(request),
        rate_limit_key_hash=_hash_identifier(key),
        limit=limit,
        window_seconds=window_seconds,
        used=int(used),
        decision=decision,
    )
    if used > limit:
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "ok": False,
                "status": "rate_limited",
                "code": "USER_REPORT_RATE_LIMITED",
                "reason_code": "user_report_rate_limited",
                "message": "Too many report attempts. Please wait a moment and try again.",
                "retry_after_seconds": window_seconds,
                "request_id": get_req_id(request),
            },
            headers={"Retry-After": str(window_seconds)},
        )


async def _assert_user_report_success_quota_available(request: Request, redis_cli, *, limit: int = 3) -> tuple[str, int]:
    scope = _user_report_scope(request)
    date_bucket = time.strftime("%Y_%m_%d", time.gmtime())
    key = f"quota:user_reports:successful:{scope}:{date_bucket}"
    used_before = int(await redis_cli.get(key) or 0)
    remaining = max(0, int(limit) - used_before)
    decision = "allow" if used_before < int(limit) else "block"
    logger.info(
        "user_report_success_quota_checked",
        request_id=get_req_id(request),
        quota_type="successful_daily_submission",
        quota_key_hash=_hash_identifier(key),
        quota_limit=int(limit),
        quota_used_before=used_before,
        quota_remaining_before=remaining,
        decision=decision,
    )
    if used_before >= int(limit):
        retry_after_seconds = _seconds_until_utc_midnight()
        logger.info(
            "user_report_submit_blocked",
            request_id=get_req_id(request),
            reason_code="daily_report_quota_exceeded",
            quota_type="successful_daily_submission",
            quota_key_hash=_hash_identifier(key),
            quota_limit=int(limit),
            quota_used=used_before,
            retry_after_seconds=retry_after_seconds,
        )
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "ok": False,
                "status": "quota_exceeded",
                "code": "DAILY_REPORT_QUOTA_EXCEEDED",
                "reason_code": "daily_report_quota_exceeded",
                "message": "Daily report limit reached. Please try again tomorrow.",
                "retry_after_seconds": retry_after_seconds,
                "request_id": get_req_id(request),
            },
            headers={"Retry-After": str(retry_after_seconds)},
        )
    return key, int(limit)


async def _increment_user_report_success_quota(request: Request, redis_cli, *, key: str, limit: int) -> None:
    quota_repo = getattr(request.app.state, "quota_repo", None)
    if not getattr(quota_repo, "redis_client", None) or quota_repo.redis_client is not redis_cli:
        quota_repo = QuotaRepository(redis_cli)

    try:
        allowed, remaining_after = await quota_repo.check_and_consume(
            key,
            int(limit),
            ttl=_seconds_until_utc_midnight(),
        )
    except RuntimeError as exc:
        logger.error(
            "user_report_success_quota_redis_failure",
            request_id=get_req_id(request),
            quota_type="successful_daily_submission",
            quota_key_hash=_hash_identifier(key),
            error=str(exc),
        )
        raise

    used_after = max(0, int(limit) - int(remaining_after))
    if not allowed:
        retry_after_seconds = _seconds_until_utc_midnight()
        logger.info(
            "user_report_submit_blocked",
            request_id=get_req_id(request),
            reason_code="daily_report_quota_exceeded",
            quota_type="successful_daily_submission",
            quota_key_hash=_hash_identifier(key),
            quota_limit=int(limit),
            quota_used=used_after,
            retry_after_seconds=retry_after_seconds,
        )
        # Quota increment happens post-persistence by design. If concurrent writes race, the
        # atomic Redis script admits only the configured limit; an over-limit persisted row can
        # still exist, but the client receives 429 and the Redis quota counter is not exceeded.
        raise HTTPException(
            status_code=http_status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "ok": False,
                "status": "quota_exceeded",
                "code": "DAILY_REPORT_QUOTA_EXCEEDED",
                "reason_code": "daily_report_quota_exceeded",
                "message": "Daily report limit reached. Please try again tomorrow.",
                "retry_after_seconds": retry_after_seconds,
                "request_id": get_req_id(request),
            },
            headers={"Retry-After": str(retry_after_seconds)},
        )

    logger.info(
        "user_report_success_quota_incremented",
        request_id=get_req_id(request),
        quota_type="successful_daily_submission",
        quota_used_after=used_after,
        quota_remaining_after=max(0, int(remaining_after)),
    )


@router.post("/user-reports", response_model=UserReportResponse)
async def user_report_submit(
    request: Request,
    data: UserReportRequest,
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    policy_engine: PolicyEngine = Depends(get_policy_engine),
):
    request_id = get_req_id(request)
    redis_cli = getattr(request.app.state, "redis", None)
    if not redis_cli:
        raise HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")
    try:
        await _check_user_report_attempt_rate_limit(request, redis_cli)
        if not data.cf_turnstile_token:
            logger.warning(
                "user_report_rejected",
                reason_code="turnstile_required",
                source="submit_report_modal",
                has_token=False,
                token_length=0,
                request_id=request_id,
            )
            return JSONResponse(
                status_code=http_status.HTTP_403_FORBIDDEN,
                content={
                    "ok": False,
                    "code": "TURNSTILE_REQUIRED",
                    "reason_code": "turnstile_required",
                    "message": "Please verify you are human before submitting.",
                    "request_id": request_id,
                },
            )
            
        client_ip = get_client_ip(request)
        is_valid = await verify_turnstile(
            token=data.cf_turnstile_token,
            client_ip=client_ip,
            anon_id=getattr(request.state, "anon_id", None),
            user_agent=request.headers.get("user-agent"),
            source="submit_report_modal",
            origin=request.headers.get("origin"),
            hostname=request.url.hostname if request.url else None,
        )
        
        if not is_valid:
            logger.warning(
                "user_report_rejected",
                reason_code="turnstile_failed",
                source="submit_report_modal",
                cloudflare_error_codes=["invalid-input-response"],
                has_token=True,
                token_length=len(data.cf_turnstile_token),
                request_id=request_id,
            )
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("feature", "user_report_submit")
                scope.set_tag("reason_code", "turnstile_failed")
                scope.set_tag("source", "submit_report_modal")
                scope.set_tag("status_code", "403")
                sentry_sdk.capture_message("user_report_turnstile_failed", level="warning")
            return JSONResponse(
                status_code=http_status.HTTP_403_FORBIDDEN,
                content={
                    "ok": False,
                    "code": "TURNSTILE_FAILED",
                    "reason_code": "turnstile_failed",
                    "message": "Verification failed. Please complete the human check again and resubmit.",
                    "request_id": request_id,
                },
            )

        validation_reason = validate_report_location(data.lat, data.lon)
        if validation_reason:
            logger.warning(
                "user_report_rejected",
                user_id=getattr(request.state, "identity_id", None),
                reason_code=validation_reason,
                source="submit_report_modal",
            )
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "status": "invalid_location",
                    "message": "Please enter a valid location before submitting a report.",
                },
            )
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
        daily_limit = USER_REPORT_DAILY_SUCCESS_LIMIT
        success_quota_key, success_quota_limit = await _assert_user_report_success_quota_available(
            request, redis_cli, limit=daily_limit
        )
        result = await ugc_report_submit(request=request, data=ugc_data, _turnstile_verified=True, quota_repo=quota_repo, policy_engine=policy_engine)
        if not result.get("duplicate", False):
            try:
                # quota incremented only after successful DB commit
                await _increment_user_report_success_quota(
                    request, redis_cli, key=success_quota_key, limit=success_quota_limit
                )
            except HTTPException as exc:
                raise
            except RuntimeError as quota_exc:
                logger.error("user_report_success_quota_increment_failed", request_id=request_id, error=str(quota_exc))
                raise HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")
            except Exception as quota_exc:
                logger.warning("user_report_success_quota_increment_failed", request_id=request_id, error=str(quota_exc))
        logger.info(
            "user_report_submitted",
            user_id=getattr(request.state, "identity_id", None),
            report_type=data.report_type,
            lat=round(data.lat, 5),
            lon=round(data.lon, 5),
            is_nearby_now=data.is_nearby_now,
            has_notes=bool(note_stripped),
            location_source=data.location_source,
            source="submit_report_modal",
        )
        
        user_id = getattr(request.state, "identity_id", "anonymous")
        capture(
            user_id,
            "user_report_submitted",
            {
                "report_type": data.report_type,
                "is_nearby_now": data.is_nearby_now,
                "has_notes": bool(note_stripped),
                "location_source": data.location_source,
                "source": "submit_report_modal",
                "duplicate": result.get("duplicate", False)
            }
        )
        _flush_posthog()
            
        status = "duplicate_report" if result.get("duplicate", False) else "report_created"
        message = (
            "This location was recently reported. Thanks for the heads up anyway."
            if result.get("duplicate", False)
            else "Report submitted. Thanks for helping others avoid noisy surprises."
        )
        return UserReportResponse(
            ok=result["ok"],
            status=status,
            report_id=result["report_id"],
            duplicate=result.get("duplicate", False),
            message=message,
        )
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
    finally:
        _flush_posthog()


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
        payload.event,
        request_id=getattr(request.state, "request_id", None),
        anon_id=getattr(request.state, "anon_id", None),
        session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
        endpoint="/api/telemetry/client-event",
        flow_type=payload.flow_type,
        surface=payload.surface,
        modal_name=payload.modal_name,
        action=payload.action,
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
    _turnstile_verified: bool = Depends(verify_turnstile_dependency),
    quota_repo: QuotaRepository = Depends(get_quota_repo),
    policy_engine: PolicyEngine = Depends(get_policy_engine),
):
    anon_id = getattr(request.state, "anon_id", None) or "unknown_anon"
    user_id = getattr(request.state, "user_id", None)
    tier = getattr(request.state, "tier", TierStatus.FREE)
    if not is_inside_app_bbox(data.lat, data.lon):
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(error="OUT_OF_BOUNDS", detail="Coordinates outside allowed area.").model_dump(),
        )
    category = REPORT_TYPE_TO_CATEGORY[data.report_type]
    severity = REPORT_TYPE_TO_SEVERITY[data.report_type]
    logger.info(
        "ugc_report_type_resolved",
        report_type=data.report_type,
        category=category,
        severity=severity,
    )
    if data.evidence_urls is not None:
        if len(data.evidence_urls) > 5:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=ErrorResponse(error="TOO_MANY_EVIDENCE_URLS", detail="Maximum 5 evidence URLs allowed.").model_dump(),
            )
        for u in data.evidence_urls:
            if len(u) > 500:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail=ErrorResponse(error="EVIDENCE_URL_TOO_LONG", detail="Evidence URL exceeds maximum length.").model_dump(),
                )

    redis_cli = getattr(request.app.state, "redis", None)
    db_engine = getattr(request.app.state, "db_engine", None)
    if not db_engine:
        raise HTTPException(status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE, detail="enforcement unavailable")

    content_hash, geo_cell, day_bucket = generate_ugc_dedup_fields(data)
    dedup_redis_key = generate_ugc_dedup_key(content_hash, geo_cell, day_bucket)
    public_id = str(uuid.uuid4())
    redis_claimed = False

    if redis_cli:
        try:
            redis_claimed = bool(
                await redis_cli.set(
                    dedup_redis_key,
                    public_id,
                    ex=UGC_DEDUP_TTL_SECONDS,
                    nx=True,
                )
            )
            if not redis_claimed:
                existing = await redis_cli.get(dedup_redis_key)
                logger.info(
                    "ugc_report_dedup_redis_duplicate",
                    report_id=existing or public_id,
                    dedup_key_hash=_hash_identifier(dedup_redis_key),
                    content_hash=content_hash,
                    geo_cell=geo_cell,
                    day_bucket=day_bucket,
                )
                return {"ok": True, "report_id": existing or public_id, "duplicate": True}
        except Exception as exc:
            redis_claimed = False
            logger.warning(
                "ugc_dedup_redis_unavailable",
                error=str(exc),
                dedup_key_hash=_hash_identifier(dedup_redis_key),
                content_hash=content_hash,
                geo_cell=geo_cell,
                day_bucket=day_bucket,
                anon_id_hash=_hash_identifier(anon_id),
                description="Redis UGC dedup unavailable; falling back to database unique constraint.",
            )
    else:
        logger.warning(
            "ugc_dedup_redis_unavailable",
            dedup_key_hash=_hash_identifier(dedup_redis_key),
            content_hash=content_hash,
            geo_cell=geo_cell,
            day_bucket=day_bucket,
            anon_id_hash=_hash_identifier(anon_id),
            description="Redis UGC dedup unavailable; falling back to database unique constraint.",
        )

    try:
        async with db_engine.begin() as conn:
            row = (
                await conn.execute(
                    UGC_INSERT_SQL,
                    {
                        "public_id": public_id,
                        "reporter_anon_id": anon_id,
                        "reporter_user_id": user_id,
                        "reporter_tier": tier_to_client(tier),
                        "title": data.title,
                        "description": data.description,
                        "category": category,
                        "severity": severity,
                        "lat": float(data.lat),
                        "lon": float(data.lon),
                        "content_hash": content_hash,
                        "geo_cell": geo_cell,
                        "day_bucket": day_bucket,
                    },
                )
            ).first()
            public_id = str(row.public_id)
    except IntegrityError as exc:
        if not _is_ugc_dedup_integrity_error(exc):
            if redis_claimed:
                try:
                    await redis_cli.delete(dedup_redis_key)
                except Exception:
                    logger.warning(
                        "ugc_report_dedup_cache_cleanup_failed",
                        dedup_key_hash=_hash_identifier(dedup_redis_key),
                    )
            logger.exception(
                "ugc_report_db_insert_failed",
                error=str(exc),
                description="Failed to insert UGC report row into database.",
            )
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=ErrorResponse(error="STORAGE_UNAVAILABLE", detail="Database unavailable.").model_dump(),
            )

        existing_public_id = public_id
        try:
            async with db_engine.begin() as conn:
                existing = (
                    await conn.execute(
                        UGC_FIND_DUPLICATE_SQL,
                        {
                            "content_hash": content_hash,
                            "geo_cell": geo_cell,
                            "day_bucket": day_bucket,
                        },
                    )
                ).first()
                if existing:
                    existing_public_id = str(existing.public_id)
        except Exception as lookup_exc:
            logger.warning(
                "ugc_report_duplicate_lookup_failed",
                error=str(lookup_exc),
                dedup_key_hash=_hash_identifier(dedup_redis_key),
            )

        if redis_cli:
            try:
                if redis_claimed:
                    await redis_cli.set(
                        dedup_redis_key,
                        existing_public_id,
                        ex=UGC_DEDUP_TTL_SECONDS,
                    )
                else:
                    await redis_cli.set(
                        dedup_redis_key,
                        existing_public_id,
                        ex=UGC_DEDUP_TTL_SECONDS,
                        nx=True,
                    )
            except Exception as redis_exc:
                logger.warning(
                    "ugc_report_dedup_cache_write_failed",
                    error=str(redis_exc),
                    report_id=existing_public_id,
                    dedup_key_hash=_hash_identifier(dedup_redis_key),
                    description="Failed to persist UGC deduplication key in Redis after DB duplicate.",
                )
        logger.info(
            "ugc_report_db_duplicate",
            report_id=existing_public_id,
            dedup_key_hash=_hash_identifier(dedup_redis_key),
            content_hash=content_hash,
            geo_cell=geo_cell,
            day_bucket=day_bucket,
        )
        return {"ok": True, "report_id": existing_public_id, "duplicate": True}
    except Exception as exc:
        if redis_claimed:
            try:
                await redis_cli.delete(dedup_redis_key)
            except Exception:
                logger.warning(
                    "ugc_report_dedup_cache_cleanup_failed",
                    dedup_key_hash=_hash_identifier(dedup_redis_key),
                )
        if "UndefinedObjectError" in str(exc) or "does not exist" in str(exc):
            logger.error(
                "ugc_report_schema_error",
                error=str(exc),
            )
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
            raise HTTPException(
                status_code=http_status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "SCHEMA_ERROR",
                    "detail": "Database schema mismatch.",
                    "retry_after_seconds": None,
                    "quota_remaining": None,
                    "error_id": None,
                },
            )
        logger.exception(
            "ugc_report_db_insert_failed",
            error=str(exc),
            description="Failed to insert UGC report row into database.",
        )
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(error="STORAGE_UNAVAILABLE", detail="Database unavailable.").model_dump(),
        )

    if data.evidence_urls and redis_cli:
        try:
            await redis_cli.set(
                f"ugc:evidence:{public_id}",
                json.dumps(data.evidence_urls),
                ex=UGC_DEDUP_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "ugc_report_evidence_cache_write_failed",
                error=str(exc),
                report_id=public_id,
                description="Failed to cache UGC evidence URLs in Redis.",
            )

    return {"ok": True, "report_id": public_id, "duplicate": False}
