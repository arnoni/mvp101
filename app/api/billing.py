import logging
import uuid
import random

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import insert, select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.models.models import FeatureFlag, FunnelEvent, SimulatedBillingPlan, SimulatedPaymentIntent, User
from app.schemas.billing import UnlockIntentRequest, UnlockIntentResponse, UnlockUiSurface
from app.services.entitlement_service import TierStatus
from app.utils.security import get_client_ip, protect_mutation, verify_turnstile
from app.utils.url import resolve_checkout_base

router = APIRouter()
logger = logging.getLogger(__name__)

SIMULATED_PAID_USERS_ALLOWED_FLAG = "simulated_paid_users_allowed_flag"


def _tier_to_funnel(tier: TierStatus | str | None) -> str:
    if tier == TierStatus.SIMULATED_PAID:
        return "simulated_paid"
    if tier == TierStatus.PASS_1_DAY:
        return "paid_1_day"
    if tier == TierStatus.PASS_3_DAY:
        return "paid_3_days"
    return "free"


def _report_funnel_failure(*, route: str, event_name: str, request: Request, exc: Exception) -> None:
    logger.error(
        "FUNNEL_EVENT_WRITE_FAILED route=%s event=%s user_id=%s session_id=%s anon_id=%s error_class=%s error=%s",
        route,
        event_name,
        getattr(request.state, "user_id", None),
        request.cookies.get(settings.SESSION_COOKIE_NAME),
        getattr(request.state, "anon_id", None),
        exc.__class__.__name__,
        str(exc),
    )
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("telemetry_kind", "funnel_event")
            scope.set_tag("route", route)
            scope.set_tag("event_name", event_name)
            sentry_sdk.capture_exception(exc)
    except Exception:
        logger.error("FUNNEL_EVENT_SENTRY_CAPTURE_FAILED route=%s event=%s", route, event_name)


@router.post('/unlock-intent', response_model=UnlockIntentResponse)
async def unlock_intent(payload: UnlockIntentRequest, request: Request):
    await protect_mutation(request)
    if not payload.turnstile_token:
        raise HTTPException(status_code=400, detail="Turnstile token required")

    is_valid_turnstile = await verify_turnstile(payload.turnstile_token, client_ip=get_client_ip(request))
    if not is_valid_turnstile:
        raise HTTPException(status_code=403, detail="Turnstile verification failed")

    db_engine = getattr(request.app.state, "db_engine", None)
    if not db_engine:
        raise HTTPException(status_code=503, detail="Database is not configured")

    email = payload.email.lower()
    anon_id = getattr(request.state, "anon_id", None)
    ab_cohort = getattr(request.state, "ab_cohort", None)
    if ab_cohort not in {"A", "B"}:
        ab_cohort = random.choice(["A", "B"])
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    effective_tier = _tier_to_funnel(getattr(request.state, "tier", TierStatus.FREE))
    ui_surface = payload.ui_surface.value if payload.ui_surface else UnlockUiSurface.HERO_UNLOCK_BUTTON.value

    async with db_engine.begin() as conn:
        flag_result = await conn.execute(
            select(FeatureFlag).where(FeatureFlag.key == SIMULATED_PAID_USERS_ALLOWED_FLAG).limit(1)
        )
        flag = flag_result.scalar_one_or_none()
        if not flag or not flag.is_enabled:
            raise HTTPException(status_code=403, detail="Simulated unlock is currently disabled")

        user_stmt = (
            pg_insert(User)
            .values(email=email, ab_cohort=ab_cohort)
            .on_conflict_do_update(index_elements=[User.email], set_={"updated_at": func.now()})
            .returning(User.id)
        )
        user_result = await conn.execute(user_stmt)
        user_id = user_result.scalar_one()

        plan_result = await conn.execute(
            select(SimulatedBillingPlan).where(
                SimulatedBillingPlan.code == payload.plan,
                SimulatedBillingPlan.is_active.is_(True),
            ).limit(1)
        )
        plan = plan_result.scalar_one_or_none()
        if not plan:
            raise HTTPException(status_code=400, detail="Invalid or inactive simulated plan")

        simulated_intent_id = str(uuid.uuid4())
        await conn.execute(
            insert(SimulatedPaymentIntent).values(
                id=simulated_intent_id,
                user_id=user_id,
                plan_code=plan.code,
                status="initiated",
                source="simulated_paid",
                upgraded_from_anon_id=anon_id,
            )
        )
        try:
            await conn.execute(
                insert(FunnelEvent).values(
                    event_name="simulated_magic_requested",
                    event_source="api",
                    event_version=1,
                    anon_id=anon_id,
                    session_id=session_id,
                    user_id=user_id,
                    effective_tier=effective_tier,
                    selected_language=request.cookies.get("dd_lang") or "en",
                    cohort=ab_cohort,
                    target_tier="simulated_paid",
                    transition_name="free_to_simulated_paid",
                    related_simulated_intent_id=simulated_intent_id,
                    ui_surface=ui_surface,
                    metadata_json={"plan_code": plan.code},
                )
            )
        except Exception as event_err:
            _report_funnel_failure(
                route="/api/billing/unlock-intent",
                event_name="simulated_magic_requested",
                request=request,
                exc=event_err,
            )

    app_origin = resolve_checkout_base(settings.APP_ORIGIN).rstrip("/")
    logger.info("SIMULATED_UNLOCK_INTENT_CREATED user=%s anon_id=%s plan=%s", email, anon_id, payload.plan)
    return UnlockIntentResponse(
        checkout_url=f"{app_origin}/?simulated_unlock=initiated",
        intent_id=simulated_intent_id,
        message="Simulated unlock initiated",
    )
