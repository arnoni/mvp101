import uuid
import random
import asyncio
import structlog

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import insert, select, func, case
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.models.models import FeatureFlag, FunnelEvent, SimulatedBillingPlan, SimulatedPaymentIntent, User
from app.schemas.billing import UnlockIntentRequest, UnlockIntentResponse, UnlockUiSurface
from app.services.analytics import capture
from app.services.entitlement_service import TierStatus
from app.services.magic_auth_service import MagicAuthService, PaymentGatewayFactory
from app.utils.security import get_client_ip, protect_mutation, verify_turnstile
from app.utils.url import resolve_checkout_base
from email_service import EmailService

router = APIRouter()
logger = structlog.get_logger(__name__)

SIMULATED_PAID_USERS_ALLOWED_FLAG = "simulated_paid_users_allowed_flag"
SIMULATED_PLAN_ALIAS_TO_HOURS = {
    "sim_1_day": 24,
}
UNLOCK_INTENT_MAGIC_LINK_TIMEOUT_SECONDS = 12


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
    request_id = getattr(request.state, "request_id", None)
    logger.info(
        "unlock_intent_received",
        request_id=request_id,
        email=payload.email.lower(),
        plan=payload.plan,
        ui_surface=payload.ui_surface.value if payload.ui_surface else UnlockUiSurface.HERO_UNLOCK_BUTTON.value,
    )
    logger.info(
        "join_research_access_unlock_intent_started",
        request_id=request_id,
        anon_id=getattr(request.state, "anon_id", None),
        session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
        endpoint="/api/billing/unlock-intent",
        status="started",
        flow_type="simulated_paid",
                surface="join_research_access_modal",
                modal_name="join_research_access_modal",
    )
    await protect_mutation(request)
    if not payload.turnstile_token:
        raise HTTPException(status_code=400, detail="Turnstile token required")

    is_valid_turnstile = await verify_turnstile(payload.turnstile_token, client_ip=get_client_ip(request))
    if not is_valid_turnstile:
        logger.warning("unlock_intent_turnstile_failed", request_id=request_id, email=payload.email.lower())
        raise HTTPException(status_code=403, detail="Turnstile verification failed")
    logger.info("unlock_intent_turnstile_verified", request_id=request_id, email=payload.email.lower())

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
    app_origin = resolve_checkout_base(settings.APP_ORIGIN).rstrip("/")

    try:
        async with db_engine.begin() as conn:
            flag_result = await conn.execute(
                select(FeatureFlag.is_enabled)
                .where(FeatureFlag.key == SIMULATED_PAID_USERS_ALLOWED_FLAG)
                .limit(1)
            )
            flag_is_enabled = flag_result.scalar_one_or_none()
            if not flag_is_enabled:
                logger.info("simulated_unlock_declined_flag_disabled", email=email, anon_id=anon_id)
                return UnlockIntentResponse(
                    ok=False,
                    status="simulated_unlock_disabled",
                    intent_id="",
                    message="Simulated unlock is currently disabled",
                    checkout_url=None,
                )

            user_stmt = (
                pg_insert(User)
                .values(email=email, ab_cohort=ab_cohort)
                .on_conflict_do_update(index_elements=[User.email], set_={"updated_at": func.now()})
                .returning(User.id)
            )
            user_result = await conn.execute(user_stmt)
            user_id = user_result.scalar_one()
            logger.info("unlock_intent_user_upserted", request_id=request_id, email=email, user_id=user_id)

            if payload.plan not in ["sim_1_day"]:
                raise HTTPException(status_code=400, detail="Unsupported simulated access duration")

            requested_duration_hours = SIMULATED_PLAN_ALIAS_TO_HOURS.get(payload.plan)
            if requested_duration_hours is not None:
                plan_result = await conn.execute(
                    select(SimulatedBillingPlan.code).where(
                        SimulatedBillingPlan.duration_hours == requested_duration_hours,
                        SimulatedBillingPlan.is_active.is_(True),
                        SimulatedBillingPlan.cohort.in_([ab_cohort, None]),
                    ).order_by(
                        case((SimulatedBillingPlan.cohort == ab_cohort, 0), else_=1),
                        SimulatedBillingPlan.code.asc(),
                    ).limit(1)
                )
            else:
                plan_result = await conn.execute(
                    select(SimulatedBillingPlan.code).where(
                        SimulatedBillingPlan.code == payload.plan,
                        SimulatedBillingPlan.is_active.is_(True),
                    ).limit(1)
                )
            plan_code = plan_result.scalar_one_or_none()
            if not plan_code:
                raise HTTPException(status_code=400, detail="Invalid or inactive simulated plan")
            logger.info("unlock_intent_plan_resolved", request_id=request_id, email=email, requested_plan=payload.plan, resolved_plan=plan_code)

            simulated_intent_id = str(uuid.uuid4())
            await conn.execute(
                insert(SimulatedPaymentIntent).values(
                    id=simulated_intent_id,
                    user_id=user_id,
                    plan_code=plan_code,
                    status="initiated",
                    source="simulated_paid",
                    upgraded_from_anon_id=anon_id,
                )
            )
            logger.info("unlock_intent_record_created", request_id=request_id, email=email, intent_id=simulated_intent_id)
            logger.info(
                "join_research_access_unlock_intent_succeeded",
                request_id=request_id,
                anon_id=anon_id,
                session_id=session_id,
                endpoint="/api/billing/unlock-intent",
                status="created",
                flow_type="simulated_paid",
                surface="join_research_access_modal",
                modal_name="join_research_access_modal",
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
                        metadata_json={"plan_code": plan_code, "email": email},
                    )
                )
            except Exception as event_err:
                _report_funnel_failure(
                    route="/api/billing/unlock-intent",
                    event_name="simulated_magic_requested",
                    request=request,
                    exc=event_err,
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "join_research_access_flow_failed",
            request_id=request_id,
            anon_id=anon_id,
            session_id=session_id,
            endpoint="/api/billing/unlock-intent",
            status="failed",
            flow_type="simulated_paid",
                surface="join_research_access_modal",
                modal_name="join_research_access_modal",
            error_code=exc.__class__.__name__,
            error_message=str(exc),
        )
        logger.exception("unlock_intent_failed", email=email, anon_id=anon_id, plan=payload.plan, error_class=exc.__class__.__name__, error_detail=str(exc))
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        raise

    email_sent = False
    try:
        redis_cli = getattr(request.app.state, "redis", None)
        auth_service = MagicAuthService(
            db=db_engine,
            redis=redis_cli,
            payment_factory=PaymentGatewayFactory(),
        )

        async def _create_and_send_magic_link() -> bool:
            logger.info("unlock_intent_magic_link_create_started", request_id=request_id, email=email, intent_id=simulated_intent_id)
            token = await auth_service.create_magic_link(email=email)
            logger.info(
                "join_research_access_magic_link_requested",
                request_id=request_id,
                anon_id=anon_id,
                session_id=session_id,
                endpoint="/api/billing/unlock-intent",
                status="token_created",
                flow_type="simulated_paid",
                surface="join_research_access_modal",
                modal_name="join_research_access_modal",
            )
            magic_link = f"{app_origin}/api/auth/magic?token={token}"
            email_service = EmailService()
            logger.info(
                "join_research_access_magic_link_requested",
                request_id=request_id,
                anon_id=anon_id,
                session_id=session_id,
                endpoint="/api/billing/unlock-intent",
                status="email_send_started",
                flow_type="simulated_paid",
                surface="join_research_access_modal",
                modal_name="join_research_access_modal",
            )
            return bool(
                await email_service.send_magic_link(
                    email=email,
                    magic_link=magic_link,
                    expire_minutes=settings.MAGICLINK_EXPIRY_MINUTES,
                )
            )

        email_sent = await asyncio.wait_for(
            _create_and_send_magic_link(),
            timeout=UNLOCK_INTENT_MAGIC_LINK_TIMEOUT_SECONDS,
        )
        logger.info(
            "join_research_access_magic_link_succeeded",
            request_id=request_id,
            anon_id=anon_id,
            session_id=session_id,
            endpoint="/api/billing/unlock-intent",
            status="email_sent",
            flow_type="simulated_paid",
                surface="join_research_access_modal",
                modal_name="join_research_access_modal",
        )
        logger.info("unlock_intent_magic_link_create_finished", request_id=request_id, email=email, intent_id=simulated_intent_id, email_sent=email_sent)
    except asyncio.TimeoutError:
        logger.error(
            "join_research_access_flow_failed",
            request_id=request_id,
            anon_id=anon_id,
            session_id=session_id,
            endpoint="/api/billing/unlock-intent",
            status="timeout",
            flow_type="simulated_paid",
                surface="join_research_access_modal",
                modal_name="join_research_access_modal",
            error_code="EMAIL_SEND_TIMEOUT",
            error_message="Magic link email timed out.",
        )
        logger.error(
            "unlock_intent_magic_email_timed_out",
            email=email,
            anon_id=anon_id,
            timeout_seconds=UNLOCK_INTENT_MAGIC_LINK_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.exception(
            "join_research_access_flow_failed",
            request_id=request_id,
            anon_id=anon_id,
            session_id=session_id,
            endpoint="/api/billing/unlock-intent",
            status="failed",
            flow_type="simulated_paid",
                surface="join_research_access_modal",
                modal_name="join_research_access_modal",
            error_code=exc.__class__.__name__,
            error_message=str(exc),
        )
        logger.exception("unlock_intent_magic_email_failed", email=email, anon_id=anon_id, error_class=exc.__class__.__name__, error_detail=str(exc))
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        except Exception:
            pass

    if email_sent:
        try:
            async with db_engine.begin() as conn:
                update_result = await conn.execute(
                    SimulatedPaymentIntent.__table__.update()
                    .where(
                        SimulatedPaymentIntent.id == simulated_intent_id,
                        SimulatedPaymentIntent.status == "initiated",
                    )
                    .values(status="magic_sent", updated_at=func.now())
                )
                if (update_result.rowcount or 0) > 0:
                    await conn.execute(
                        insert(FunnelEvent).values(
                            event_name="simulated_magic_sent",
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
                            metadata_json={"email": email},
                        )
                    )
                    capture(str(user_id), "simulated_magic_sent", {"path": "billing"})
        except Exception as event_err:
            _report_funnel_failure(
                route="/api/billing/unlock-intent",
                event_name="simulated_magic_sent",
                request=request,
                exc=event_err,
            )

    logger.info("SIMULATED_UNLOCK_INTENT_CREATED user=%s anon_id=%s plan=%s", email, anon_id, payload.plan)
    logger.info(
        "unlock_intent_response_ready",
        request_id=request_id,
        email=email,
        intent_id=simulated_intent_id,
        status="magic_link_sent" if email_sent else "intent_created",
    )
    return UnlockIntentResponse(
        ok=True,
        status="magic_link_sent" if email_sent else "intent_created",
        intent_id=simulated_intent_id,
        message="If this email is eligible, we sent a new access link." if email_sent else "Intent created.",
        checkout_url=None,
    )
