import time
import secrets
import json
import uuid
import hashlib
import random
import httpx
import structlog
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Request, Response, Depends
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy import insert, select, text, update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.core.keys import KeyBuilder
from app.api.dependencies import scope_to_user
from app.models.models import FunnelEvent, MagicLinkToken, SimulatedBillingPlan, SimulatedPaymentIntent, SimulatedUserPass, User
from app.services.analytics import capture
from app.services.entitlement_service import TierStatus
from app.services.magic_auth_service import MagicAuthService, PaymentGatewayFactory
from app.utils.security import get_client_ip, verify_turnstile
from app.utils.url import resolve_checkout_base
from email_service import EmailService

logger = structlog.get_logger(__name__)
router = APIRouter()

# ==========================================
# SCHEMAS
# ==========================================

class LoginRequest(BaseModel):
    email: EmailStr
    purchase_id: Optional[str] = None
    provider: Optional[str] = None  # "paddle", "lemonsqueezy", or "dodo"

class AuthResponse(BaseModel):
    message: str

class LogoutResponse(BaseModel):
    message: str


class MagicLinkRequest(BaseModel):
    email: EmailStr
    turnstile_token: Optional[str] = None
    intent_id: Optional[str] = None


def _tier_to_funnel(tier: TierStatus | str | None) -> str:
    if tier == TierStatus.SIMULATED_PAID:
        return "simulated_paid"
    if tier == TierStatus.PASS_1_DAY:
        return "paid_1_day"
    if tier == TierStatus.PASS_3_DAY:
        return "paid_3_days"
    return "free"


def _report_funnel_failure(
    *,
    route: str,
    event_name: str,
    request: Request,
    exc: Exception,
) -> None:
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
            scope.set_extra("user_id", str(getattr(request.state, "user_id", None)))
            scope.set_extra("session_id", request.cookies.get(settings.SESSION_COOKIE_NAME))
            scope.set_extra("anon_id", getattr(request.state, "anon_id", None))
            sentry_sdk.capture_exception(exc)
    except Exception:
        logger.error("FUNNEL_EVENT_SENTRY_CAPTURE_FAILED route=%s event=%s", route, event_name)


def _pass_duration_days(plan_code: str | None) -> int:
    code = (plan_code or "").lower()
    if code.startswith("3_day"):
        return 3
    return 1


async def _carry_forward_anon_quota_usage(redis, *, anon_id: str | None, user_id: str | None) -> int:
    if not redis or not anon_id or not user_id:
        return 0
    anon_key = KeyBuilder.quota_rolling24h("anon", anon_id)
    paid_key = KeyBuilder.quota_rolling24h("paid", user_id)
    try:
        anon_raw = await redis.get(anon_key)
        used_count = int(anon_raw or 0)
    except Exception:
        logger.warning("quota_usage_carry_forward_read_failed", anon_id=anon_id, user_id=user_id)
        return 0
    if used_count <= 0:
        return 0
    try:
        paid_raw = await redis.get(paid_key)
        paid_existing = int(paid_raw or 0)
    except Exception:
        paid_existing = 0
    if paid_existing >= used_count:
        return 0
    ttl_seconds = 86400
    try:
        anon_ttl = await redis.ttl(anon_key)
        if isinstance(anon_ttl, int) and anon_ttl > 0:
            ttl_seconds = anon_ttl
    except Exception:
        pass
    try:
        await redis.set(paid_key, used_count, ex=ttl_seconds)
        logger.info(
            "quota_usage_carried_forward",
            anon_id=anon_id,
            user_id=user_id,
            used_count=used_count,
            ttl_seconds=ttl_seconds,
        )
    except Exception:
        logger.warning("quota_usage_carry_forward_write_failed", anon_id=anon_id, user_id=user_id)
        return 0
    return used_count


async def _fetch_dodo_checkout_status(checkout_id: str) -> dict | None:
    if not settings.DODO_API_KEY:
        return None
    base_url = "https://api.dodopayments.com/v1" if settings.ENV == "production" else "https://test.dodopayments.com/v1"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                f"{base_url}/checkouts/{checkout_id}",
                headers={"Authorization": f"Bearer {settings.DODO_API_KEY}"},
            )
            if res.status_code >= 400:
                return None
            return res.json()
    except Exception:
        logger.warning("dodo_checkout_status_fetch_failed", checkout_id=checkout_id, error="unexpected_exception")
        return None


async def _send_magic_link_email(*, email: str, db_engine, redis_cli) -> bool:
    try:
        service = MagicAuthService(
            db=db_engine,
            redis=redis_cli,
            payment_factory=PaymentGatewayFactory(),
        )
        token = await service.create_magic_link(email=email)
        app_origin = settings.APP_ORIGIN or "http://localhost:8000"
        magic_link = f"{app_origin}/api/auth/magic?token={token}"
        email_service = EmailService()
        sent = bool(
            await email_service.send_magic_link(
                email=email,
                magic_link=magic_link,
                expire_minutes=settings.MAGICLINK_EXPIRY_MINUTES,
            )
        )
        if not sent:
            logger.warning("magic_link_email_send_returned_false", email=email)
        return sent
    except Exception as exc:
        logger.error(
            "magic_link_email_send_exception",
            email=email,
            error_class=exc.__class__.__name__,
            error_detail=str(exc),
        )
        return False


async def _find_latest_simulated_intent(conn, *, email: str, intent_id: str | None = None):
    sql = """
        SELECT spi.id AS intent_id, spi.user_id AS user_id, spi.status AS status
        FROM simulated_payment_intents spi
        JOIN users u ON u.id = spi.user_id
        WHERE LOWER(u.email) = :email
          AND spi.status IN ('initiated', 'magic_sent')
    """
    params = {"email": email}
    if intent_id:
        sql += " AND spi.id = :intent_id"
        params["intent_id"] = intent_id
    sql += " ORDER BY spi.created_at DESC LIMIT 1"
    result = await conn.execute(text(sql), params)
    return result.mappings().first()


async def _resend_magic_link_impl(payload: MagicLinkRequest, request: Request, *, enforce_turnstile: bool) -> AuthResponse:
    # Intentionally generic response to avoid account enumeration.
    generic_response = AuthResponse(message="If this email is eligible, we sent a new access link.")
    request_id = getattr(request.state, "request_id", None)
    redis_cli = getattr(request.app.state, "redis", None)
    db_engine = getattr(request.app.state, "db_engine", None)
    logger.info(
        "magic_link_resend_received",
        request_id=request_id,
        email=payload.email.lower(),
        enforce_turnstile=enforce_turnstile,
        has_intent_id=bool(payload.intent_id),
    )
    if not redis_cli or not db_engine:
        logger.warning("magic_link_unavailable_services", has_redis=bool(redis_cli), has_db=bool(db_engine))
        return generic_response

    email = payload.email.lower()
    ip = get_client_ip(request)
    if enforce_turnstile:
        if not payload.turnstile_token:
            logger.info("magic_link_turnstile_missing", email=email)
            return generic_response
        turnstile_ok = await verify_turnstile(payload.turnstile_token, client_ip=ip)
        if not turnstile_ok:
            logger.info("magic_link_turnstile_invalid", email=email, client_ip=ip)
            return generic_response
        logger.info("magic_link_turnstile_valid", request_id=request_id, email=email, client_ip=ip)

    cooldown_key = f"magic_resend:cooldown:{email}"
    count_key = f"magic_resend:count:{email}:{ip}"
    ip_count_key = f"magic_ip_limit:{ip}"

    try:
        ip_count = await redis_cli.incr(ip_count_key)
        if ip_count == 1:
            await redis_cli.expire(ip_count_key, 3600)
        if int(ip_count) > 10:
            logger.warning("magic_link_ip_rate_limit_exceeded", ip=ip, count=int(ip_count))
            return generic_response

        if await redis_cli.get(cooldown_key):
            logger.info("magic_link_cooldown_active", email=email)
            return generic_response
        count = await redis_cli.incr(count_key)
        if count == 1:
            await redis_cli.expire(count_key, 180)
        if int(count) > 2:
            await redis_cli.set(cooldown_key, "1", ex=180)
            logger.warning("magic_link_rate_limited", email=email, client_ip=ip, count=int(count))
            return generic_response
    except Exception as exc:
        logger.error("magic_link_rate_limit_check_failed", email=email, client_ip=ip, error=str(exc))
        return generic_response

    active_pass = None
    simulated_intent = None
    async with db_engine.connect() as conn:
        simulated_intent = await _find_latest_simulated_intent(
            conn,
            email=email,
            intent_id=payload.intent_id,
        )
        if payload.intent_id and simulated_intent is None:
            ownership_result = await conn.execute(
                text(
                    """
                    SELECT u.email
                    FROM simulated_payment_intents spi
                    JOIN users u ON u.id = spi.user_id
                    WHERE spi.id = :intent_id
                    LIMIT 1
                    """
                ),
                {"intent_id": payload.intent_id},
            )
            ownership_row = ownership_result.mappings().first()
            if ownership_row and str(ownership_row["email"]).lower() != email:
                logger.warning("magic_link_simulated_intent_ownership_mismatch", email=email, intent_id=payload.intent_id)
                return generic_response

        active_pass_result = await conn.execute(
            text(
                """
                SELECT up.id, up.plan_code, up.expires_at, u.id AS user_id
                FROM user_passes up
                JOIN users u ON u.id = up.user_id
                WHERE u.email = :email
                  AND up.status = 'active'
                  AND up.expires_at > NOW()
                ORDER BY up.expires_at DESC
                LIMIT 1
                """
            ),
            {"email": email},
        )
        active_pass = active_pass_result.mappings().first()
        pending_intent_sql = """
            SELECT pi.id, pi.user_id, pi.plan_code, pi.provider_intent_id, pi.status
            FROM payment_intents pi
            JOIN users u ON u.id = pi.user_id
            WHERE u.email = :email
              AND pi.status IN ('initiated', 'pending')
        """
        params = {"email": email}
        if payload.intent_id:
            pending_intent_sql += " AND pi.id = :intent_id ORDER BY pi.created_at DESC LIMIT 1"
            params["intent_id"] = payload.intent_id
        else:
            pending_intent_sql += " ORDER BY pi.created_at DESC LIMIT 1"
        pending_intent_result = await conn.execute(text(pending_intent_sql), params)
        pending_intent = pending_intent_result.mappings().first()
        if payload.intent_id and not pending_intent:
            ownership_result = await conn.execute(
                text(
                    """
                    SELECT u.email
                    FROM payment_intents pi
                    JOIN users u ON u.id = pi.user_id
                    WHERE pi.id = :intent_id
                    LIMIT 1
                    """
                ),
                {"intent_id": payload.intent_id},
            )
            ownership_row = ownership_result.mappings().first()
            if ownership_row and str(ownership_row["email"]).lower() != email:
                logger.warning(
                    "AUTH_RESEND_INTENT_OWNERSHIP_MISMATCH: "
                    f"requested_email={email} intent_id={payload.intent_id}"
                )
                return generic_response

    logger.info(
        "magic_link_resend_lookup_completed",
        request_id=request_id,
        email=email,
        has_simulated_intent=bool(simulated_intent),
        has_active_pass=bool(active_pass),
        has_pending_intent=bool(pending_intent),
    )

    if simulated_intent:
        try:
            logger.info("magic_link_resend_simulated_send_started", request_id=request_id, email=email, intent_id=str(simulated_intent["intent_id"]))
            sent = await _send_magic_link_email(email=email, db_engine=db_engine, redis_cli=redis_cli)
            if not sent:
                logger.warning("magic_link_send_failed_simulated", email=email, intent_id=str(simulated_intent["intent_id"]))
                return generic_response
            async with db_engine.begin() as conn:
                update_result = await conn.execute(
                    update(SimulatedPaymentIntent)
                    .where(
                        SimulatedPaymentIntent.id == simulated_intent["intent_id"],
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
                            anon_id=getattr(request.state, "anon_id", None),
                            session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
                            user_id=simulated_intent["user_id"],
                            effective_tier=_tier_to_funnel(getattr(request.state, "tier", TierStatus.FREE)),
                            selected_language=request.cookies.get("dd_lang") or "en",
                            cohort=getattr(request.state, "ab_cohort", None),
                            target_tier="simulated_paid",
                            transition_name="free_to_simulated_paid",
                            related_simulated_intent_id=simulated_intent["intent_id"],
                            ui_surface="user_access_modal",
                            metadata_json={"email": email},
                        )
                    )
                    capture(str(simulated_intent["user_id"]), "simulated_magic_sent", {"path": "resend"})
            logger.info("magic_link_resend_simulated_send_finished", request_id=request_id, email=email, intent_id=str(simulated_intent["intent_id"]), sent=True)
            return generic_response
        except Exception as exc:
            logger.exception("magic_link_simulated_flow_failed", email=email, error=str(exc))
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(exc)
            except Exception:
                pass
            return generic_response

    if not active_pass:
        if pending_intent and pending_intent.get("provider_intent_id"):
            checkout_data = await _fetch_dodo_checkout_status(str(pending_intent["provider_intent_id"]))
            if checkout_data:
                raw_status = str(
                    checkout_data.get("status")
                    or checkout_data.get("payment_status")
                    or checkout_data.get("data", {}).get("status")
                    or ""
                ).lower()
                if raw_status in {"paid", "succeeded", "completed"}:
                    async with db_engine.begin() as conn:
                        locked_intent_result = await conn.execute(
                            text(
                                """
                                SELECT id, plan_code, provider_intent_id, status
                                FROM payment_intents
                                WHERE id = :id
                                FOR UPDATE
                                """
                            ),
                            {"id": pending_intent["id"]},
                        )
                        locked_intent = locked_intent_result.mappings().first()
                        if not locked_intent:
                            return generic_response
                        locked_status = str(locked_intent["status"]).lower()
                        if locked_status not in {"initiated", "pending"}:
                            return generic_response

                        duration_days_result = await conn.execute(
                            text(
                                """
                                SELECT duration_days
                                FROM billing_plans
                                WHERE code = :code
                                LIMIT 1
                                """
                            ),
                            {"code": locked_intent["plan_code"]},
                        )
                        duration_days = duration_days_result.scalar() or _pass_duration_days(locked_intent["plan_code"])
                        inserted_pass = await conn.execute(
                            text(
                                """
                                INSERT INTO user_passes (
                                    user_id,
                                    plan_code,
                                    provider_payment_id,
                                    amount_paid_cents,
                                    status,
                                    expires_at
                                )
                                VALUES (
                                    :user_id,
                                    :plan_code,
                                    :provider_payment_id,
                                    (
                                        SELECT amount_cents
                                        FROM payment_intents
                                        WHERE id = :intent_id
                                    ),
                                    'active',
                                    (
                                        GREATEST(
                                            COALESCE(
                                                (
                                                    SELECT MAX(expires_at)
                                                    FROM user_passes
                                                    WHERE user_id = :user_id
                                                      AND status = 'active'
                                                ),
                                                NOW()
                                            ),
                                            NOW()
                                        ) + (:duration_days * INTERVAL '1 day')
                                    )
                                )
                                ON CONFLICT (provider_payment_id) DO UPDATE
                                SET updated_at = NOW()
                                RETURNING id, plan_code, expires_at, user_id
                                """
                            ),
                            {
                                "user_id": locked_intent["user_id"],
                                "plan_code": locked_intent["plan_code"],
                                "provider_payment_id": str(locked_intent["provider_intent_id"]),
                                "intent_id": locked_intent["id"],
                                "duration_days": int(duration_days),
                            },
                        )
                        active_pass = inserted_pass.mappings().first()
                        await conn.execute(
                            text(
                                """
                                UPDATE payment_intents
                                SET status = 'paid', updated_at = NOW()
                                WHERE id = :id
                                """
                            ),
                            {"id": pending_intent["id"]},
                        )
        if not active_pass:
            # Intent exists but is not yet paid; keep generic response without forcing cooldown escalation.
            if pending_intent:
                await redis_cli.set(cooldown_key, "1", ex=30)
        return generic_response

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = func.now() + text("interval '10 minutes'")
    try:
        async with db_engine.begin() as conn:
            await conn.execute(
                insert(MagicLinkToken).values(
                    user_id=active_pass["user_id"],
                    email=email,
                    token_hash=token_hash,
                    expires_at=expires_at,
                )
            )
    except Exception as exc:
        logger.error("magic_link_token_insert_failed", email=email, error=str(exc))
        return generic_response

    app_origin = settings.APP_ORIGIN or "http://localhost:8000"
    magic_link = f"{app_origin}/api/auth/magic?token={raw_token}"
    try:
        email_service = EmailService()
        sent = await email_service.send_magic_link(
            email=email,
            magic_link=magic_link,
            expire_minutes=10,
        )
        if not sent:
            logger.warning("magic_link_send_failed_real", email=email, reason="provider_returned_false")
            return generic_response
        logger.info("magic_link_resend_real_send_finished", request_id=request_id, email=email, sent=True)
    except Exception as exc:
        logger.error(
            "magic_link_send_failed_real",
            email=email,
            reason="exception",
            error_class=exc.__class__.__name__,
            error_detail=str(exc),
        )
        return generic_response
    logger.info("magic_link_resend_response_ready", request_id=request_id, email=email)
    return generic_response


@router.post("/magic-link", response_model=AuthResponse, status_code=200)
async def resend_magic_link(payload: MagicLinkRequest, request: Request):
    return await _resend_magic_link_impl(payload, request, enforce_turnstile=True)


# ==========================================
# ROUTER & DEPENDENCIES
# ==========================================

def get_auth_service(request: Request) -> MagicAuthService:
    """Dependency injector for the auth service."""
    engine = getattr(request.app.state, "db_engine", None)
    redis_cli = getattr(request.app.state, "redis", None)
    
    if not engine or not redis_cli:
        raise HTTPException(status_code=503, detail="Database or cache service unavailable")
    
    return MagicAuthService(
        db=engine,
        redis=redis_cli,
        payment_factory=PaymentGatewayFactory()
    )


# ==========================================
# ENDPOINTS
# ==========================================

# DEPRECATED — retained for backwards compatibility only.
# Turnstile enforcement is required. Do not remove enforcement.
@router.post("/login", response_model=AuthResponse, status_code=200)
async def login(
    payload: LoginRequest,
    request: Request,
    service: MagicAuthService = Depends(get_auth_service)
):
    """
    Initiates magic link flow: creates a token (optionally tied to a MoR purchase) and "sends" it.
    """
    try:
        logger.warning("AUTH_LOGIN_DEPRECATED: /api/auth/login is deprecated; use /api/auth/magic-link")
        return await _resend_magic_link_impl(
            MagicLinkRequest(email=payload.email),
            request,
            enforce_turnstile=True,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"AUTH_LOGIN_CRITICAL_FAILURE: {e}")
        raise HTTPException(status_code=500, detail="Failed to process authentication request")


@router.get("/magic", include_in_schema=False)
async def magic_landing(
    token: str,
    request: Request,
    response: Response,
    service: MagicAuthService = Depends(get_auth_service)
):
    """
    Consumes the magic link clicked from the user's email.
    """
    app_origin = resolve_checkout_base(settings.APP_ORIGIN).rstrip("/")
    redis = service.redis
    db = service.db

    if redis is None:
        return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_SESSION_CACHE_UNAVAILABLE", status_code=303)

    try:
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        try:
            async with db.begin() as conn:
                token_result = await conn.execute(
                    select(MagicLinkToken).where(MagicLinkToken.token_hash == token_hash).limit(1)
                )
                token_row = token_result.fetchone()
                if token_row is None or token_row.redeemed_at is not None or token_row.expires_at <= datetime.now(timezone.utc):
                    logger.warning("AUTH_MAGIC_INVALID_OR_EXPIRED_LINK: hash=%s", token_hash[:8])
                    return RedirectResponse(url=f"{app_origin}/?error=invalid_link", status_code=303)

                await conn.execute(
                    update(MagicLinkToken)
                    .where(MagicLinkToken.id == token_row.id)
                    .values(redeemed_at=func.now())
                )

                user_result = await conn.execute(
                    pg_insert(User)
                    .values(
                        email=token_row.email.lower(),
                        ab_cohort=(
                            getattr(request.state, "ab_cohort", None)
                            if getattr(request.state, "ab_cohort", None) in {"A", "B"}
                            else random.choice(["A", "B"])
                        ),
                    )
                    .on_conflict_do_update(
                        index_elements=[User.email],
                        set_={"last_login": func.now(), "updated_at": func.now()},
                    )
                    .returning(User.id, User.email)
                )
                user_row = user_result.first()
                user_id_uuid = uuid.UUID(str(user_row.id))
                user_id = str(user_id_uuid)
                email = user_row.email

                pending_intent_result = await conn.execute(
                    scope_to_user(
                        select(SimulatedPaymentIntent)
                        .where(SimulatedPaymentIntent.status.in_(["initiated", "magic_sent"]))
                        .order_by(SimulatedPaymentIntent.created_at.desc())
                        .limit(1),
                        model_user_id_column=SimulatedPaymentIntent.user_id,
                        current_user_id=user_id_uuid,
                    )
                )
                pending_intent = pending_intent_result.fetchone()
                if pending_intent is not None:
                    plan_result = await conn.execute(
                        select(SimulatedBillingPlan.duration_hours)
                        .where(SimulatedBillingPlan.code == pending_intent.plan_code)
                        .limit(1)
                    )
                    duration_hours = int(plan_result.scalar_one_or_none() or 24)
                    await conn.execute(
                        update(SimulatedPaymentIntent)
                        .where(SimulatedPaymentIntent.id == pending_intent.id)
                        .values(status="activated", activated_at=func.now(), updated_at=func.now())
                    )
                    pass_result = await conn.execute(
                        insert(SimulatedUserPass).values(
                            user_id=user_id_uuid,
                            plan_code=pending_intent.plan_code,
                            simulated_intent_id=pending_intent.id,
                            status="active",
                            expires_at=func.now() + text(f"interval '{duration_hours} hours'"),
                        )
                        .returning(SimulatedUserPass.id)
                    )
                    pass_row = pass_result.first()
                    success_counter_result = await conn.execute(
                        update(User)
                        .where(User.id == user_id_uuid)
                        .values(
                            join_research_aggregated_success_count=(
                                func.coalesce(User.join_research_aggregated_success_count, 0) + 1
                            ),
                            updated_at=func.now(),
                        )
                        .returning(User.join_research_aggregated_success_count)
                    )
                    aggregated_success_count = int(success_counter_result.scalar_one() or 0)
                    anon_id = getattr(request.state, "anon_id", None)
                    carried_forward_credits = await _carry_forward_anon_quota_usage(
                        redis,
                        anon_id=getattr(request.state, "anon_id", None),
                        user_id=user_id,
                    )
                    if carried_forward_credits > 0:
                        try:
                            await conn.execute(
                                insert(FunnelEvent).values(
                                    event_name="anon_quota_usage_carried_forward",
                                    event_source="api",
                                    event_version=1,
                                    anon_id=getattr(request.state, "anon_id", None),
                                    session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
                                    user_id=user_id_uuid,
                                    effective_tier="simulated_paid",
                                    selected_language=request.cookies.get("dd_lang") or "en",
                                    cohort=getattr(request.state, "ab_cohort", None),
                                    target_tier="simulated_paid",
                                    transition_name="free_to_simulated_paid",
                                    related_simulated_intent_id=pending_intent.id,
                                    related_simulated_pass_id=(pass_row.id if pass_row else None),
                                    ui_surface="user_access_modal",
                                    metadata_json={
                                        "carried_forward_credits": carried_forward_credits,
                                        "plan_code": pending_intent.plan_code,
                                    },
                                )
                            )
                        except Exception as event_err:
                            _report_funnel_failure(
                                route="/api/auth/magic",
                                event_name="anon_quota_usage_carried_forward",
                                request=request,
                                exc=event_err,
                            )
                    try:
                        await conn.execute(
                            insert(FunnelEvent).values(
                                event_name="simulated_pass_activated",
                                event_source="api",
                                event_version=1,
                                anon_id=getattr(request.state, "anon_id", None),
                                session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
                                user_id=user_id_uuid,
                                effective_tier="simulated_paid",
                                selected_language=request.cookies.get("dd_lang") or "en",
                                cohort=getattr(request.state, "ab_cohort", None),
                                target_tier="simulated_paid",
                                transition_name="free_to_simulated_paid",
                                related_simulated_intent_id=pending_intent.id,
                                related_simulated_pass_id=(pass_row.id if pass_row else None),
                                ui_surface="user_access_modal",
                                metadata_json={"plan_code": pending_intent.plan_code, "upgraded_from_anon_id": str(anon_id) if anon_id else None},
                            )
                        )
                        capture(str(user_id_uuid), "simulated_pass_activated", {"plan_code": pending_intent.plan_code, "upgraded_from_anon_id": str(anon_id) if anon_id else None})
                    except Exception as event_err:
                        _report_funnel_failure(
                            route="/api/auth/magic",
                            event_name="simulated_pass_activated",
                            request=request,
                            exc=event_err,
                        )
                    try:
                        await conn.execute(
                            insert(FunnelEvent).values(
                                event_name="join_research_aggregated_success",
                                event_source="api",
                                event_version=1,
                                anon_id=getattr(request.state, "anon_id", None),
                                session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
                                user_id=user_id_uuid,
                                effective_tier="simulated_paid",
                                selected_language=request.cookies.get("dd_lang") or "en",
                                cohort=getattr(request.state, "ab_cohort", None),
                                target_tier="simulated_paid",
                                transition_name="free_to_simulated_paid",
                                related_simulated_intent_id=pending_intent.id,
                                related_simulated_pass_id=(pass_row.id if pass_row else None),
                                ui_surface="user_access_modal",
                                metadata_json={
                                    "plan_code": pending_intent.plan_code,
                                    "join_research_aggregated_success_count": aggregated_success_count,
                                    "upgraded_from_anon_id": str(anon_id) if anon_id else None,
                                },
                            )
                        )
                        capture(str(user_id_uuid), "join_research_aggregated_success", {"join_research_aggregated_success_count": aggregated_success_count, "upgraded_from_anon_id": str(anon_id) if anon_id else None})
                    except Exception as event_err:
                        _report_funnel_failure(
                            route="/api/auth/magic",
                            event_name="join_research_aggregated_success",
                            request=request,
                            exc=event_err,
                        )
        except Exception as e:
            logger.exception("AUTH_MAGIC_DB_ACTIVATION_FAILED", error_class=e.__class__.__name__, error_detail=str(e))
            return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_DB_ERROR&msg={str(e)[:50]}", status_code=303)

        # 4. Create durable session identifier for the browser
        session_id = secrets.token_urlsafe(32)
        session_ttl = 86400 * 3  # 3 days
        
        session_data = {
            "user_id": user_id, # This is now definitely a string
            "email": email,
            "auth_time": int(time.time()),
            "csrf_token": secrets.token_hex(16)
        }
        
        try:
            payload_json = json.dumps(session_data)
            await redis.set(KeyBuilder.session(session_id), payload_json, ex=session_ttl)
        except TypeError as te:
            logger.error(f"AUTH_MAGIC_SESSION_SERIALIZATION_FAILED: {te} | data={session_data}")
            return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_SERIALIZATION_ERROR", status_code=303)
        except Exception as e:
            logger.error(f"AUTH_MAGIC_SESSION_STORE_FAILED: {e}")
            return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_SESSION_ERROR&msg={str(e)[:50]}", status_code=303)

        # 5. Redirect to UI with success state and secure HttpOnly cookie
        redirect_response = RedirectResponse(url=f"{app_origin}/?magic_success=1", status_code=303)
        
        redirect_response.set_cookie(
            key=settings.SESSION_COOKIE_NAME,
            value=session_id,
            httponly=True,
            samesite="lax",
            secure=(settings.ENV == "production"),
            max_age=session_ttl,
            path="/"
        )

        redirect_response.delete_cookie(settings.ANON_COOKIE_NAME)

        try:
            entitlement_key = KeyBuilder.entitlement_status(user_id)
            await redis.delete(entitlement_key)
            logger.info(f"AUTH_CACHE_BUST: Cleared entitlement cache for user {user_id}")
        except Exception as cache_err:
            logger.warning(f"AUTH_CACHE_BUST_FAILED: user={user_id} err={cache_err}")
        
        logger.info(f"AUTH_SUCCESS: ✅ Auth successful for {email} via atomic magic link")
        return redirect_response

    except Exception as e:
        logger.exception(f"AUTH_MAGIC_CRITICAL_FAILURE: {e}")
        return RedirectResponse(url=f"{app_origin}/?error=system_error&code=AUTH_MAGIC_UNKNOWN_ERROR", status_code=303)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    service: MagicAuthService = Depends(get_auth_service)
):
    """
    Invalidates session: safely deletes the Redis record and clears the client cookie.
    """
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    
    if session_id:
        from app.core.keys import KeyBuilder
        await service.redis.delete(KeyBuilder.session(session_id))
        logger.debug(f"🗑️ Session invalidated in Redis: {session_id[:8]}...")
    
    response.delete_cookie(
        key=settings.SESSION_COOKIE_NAME,
        path="/",
        secure=(settings.ENV == "production"),
        httponly=True,
        samesite="lax"
    )
    
    return LogoutResponse(message="Logged out successfully")
