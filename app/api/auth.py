import time
import secrets
import json
import logging
import hashlib
import httpx
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
from app.services.entitlement_service import TierStatus
from app.services.magic_auth_service import MagicAuthService, PaymentGatewayFactory
from app.utils.security import get_client_ip, verify_turnstile
from app.utils.url import resolve_checkout_base
from email_service import EmailService

logger = logging.getLogger(__name__)
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
        return None


@router.post("/magic-link", response_model=AuthResponse, status_code=200)
async def resend_magic_link(payload: MagicLinkRequest, request: Request):
    # Intentionally generic response to avoid account enumeration.
    generic_response = AuthResponse(message="If the email exists, a magic link has been sent.")
    redis_cli = getattr(request.app.state, "redis", None)
    db_engine = getattr(request.app.state, "db_engine", None)
    if not redis_cli or not db_engine:
        return generic_response

    email = payload.email.lower()
    if not payload.turnstile_token:
        return generic_response
    ip = get_client_ip(request)
    turnstile_ok = await verify_turnstile(payload.turnstile_token, client_ip=ip)
    if not turnstile_ok:
        return generic_response

    cooldown_key = f"magic_resend:cooldown:{email}"
    count_key = f"magic_resend:count:{email}:{ip}"

    try:
        if await redis_cli.get(cooldown_key):
            return generic_response
        count = await redis_cli.incr(count_key)
        if count == 1:
            await redis_cli.expire(count_key, 180)
        if int(count) > 2:
            await redis_cli.set(cooldown_key, "1", ex=180)
            return generic_response
    except Exception:
        return generic_response

    active_pass = None
    async with db_engine.connect() as conn:
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
    except Exception:
        return generic_response

    app_origin = settings.APP_ORIGIN or "http://localhost:8000"
    magic_link = f"{app_origin}/api/auth/magic?token={raw_token}"
    try:
        email_service = EmailService()
        await email_service.send_magic_link(
            email=email,
            magic_link=magic_link,
            expire_minutes=10,
        )
    except Exception:
        return generic_response
    return generic_response


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
        # Pass the purchase_id & provider to bind the payment strictly to this auth token
        token = await service.create_magic_link(
            email=payload.email,
            purchase_id=payload.purchase_id,
            provider=payload.provider
        )
        
        # Build the callback URL
        app_origin = settings.APP_ORIGIN or "http://localhost:8000"
        magic_link = f"{app_origin}/api/auth/magic?token={token}"
        
        # Send via Resend
        email_service = EmailService()
        sent = await email_service.send_magic_link(
            email=payload.email, 
            magic_link=magic_link,
            expire_minutes=settings.MAGICLINK_EXPIRY_MINUTES
        )
        
        if not sent:
            # Fallback for dev environment if Resend is not configured
            if settings.ENV == "development":
                logger.info(f"🔐 [DEV FALLBACK] MAGIC LINK for {payload.email}: {magic_link}")
                return AuthResponse(message="Magic link logged to console (Dev Mode).")
            raise HTTPException(status_code=500, detail="Failed to send magic link email")

        try:
            async with service.db.begin() as conn:
                user_result = await conn.execute(
                    select(User.id).where(User.email == payload.email.lower()).limit(1)
                )
                user_id = user_result.scalar_one_or_none()
                if user_id is not None:
                    latest_intent_result = await conn.execute(
                        select(SimulatedPaymentIntent.id)
                        .where(
                            SimulatedPaymentIntent.user_id == user_id,
                            SimulatedPaymentIntent.status == "initiated",
                        )
                        .order_by(SimulatedPaymentIntent.created_at.desc())
                        .limit(1)
                    )
                    latest_intent_id = latest_intent_result.scalar_one_or_none()
                    if latest_intent_id is not None:
                        update_result = await conn.execute(
                            update(SimulatedPaymentIntent)
                            .where(
                                SimulatedPaymentIntent.id == latest_intent_id,
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
                                    user_id=user_id,
                                    effective_tier=_tier_to_funnel(getattr(request.state, "tier", TierStatus.FREE)),
                                    selected_language=request.cookies.get("dd_lang") or "en",
                                    target_tier="simulated_paid",
                                    transition_name="free_to_simulated_paid",
                                    related_simulated_intent_id=latest_intent_id,
                                    ui_surface="user_access_modal",
                                    metadata_json={"email": payload.email.lower()},
                                )
                            )
        except Exception as emit_err:
            _report_funnel_failure(
                route="/api/auth/login",
                event_name="simulated_magic_sent",
                request=request,
                exc=emit_err,
            )
        
        return AuthResponse(message="Magic link sent. Check your inbox.")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"AUTH_LOGIN_CRITICAL_FAILURE: {e}")
        raise HTTPException(status_code=500, detail="Failed to process authentication request")


@router.get("/magic", include_in_schema=False)
async def magic_landing(
    token: str,
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
                token_row = token_result.scalar_one_or_none()
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
                    .values(email=token_row.email.lower())
                    .on_conflict_do_update(
                        index_elements=[User.email],
                        set_={"last_login": func.now(), "updated_at": func.now()},
                    )
                    .returning(User.id, User.email)
                )
                user_row = user_result.first()
                user_id = str(user_row.id)
                email = user_row.email

                pending_intent_result = await conn.execute(
                    scope_to_user(
                        select(SimulatedPaymentIntent)
                        .where(SimulatedPaymentIntent.status.in_(["initiated", "magic_sent"]))
                        .order_by(SimulatedPaymentIntent.created_at.desc())
                        .limit(1),
                        model_user_id_column=SimulatedPaymentIntent.user_id,
                        current_user_id=user_row.id,
                    )
                )
                pending_intent = pending_intent_result.scalar_one_or_none()
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
                            user_id=user_row.id,
                            plan_code=pending_intent.plan_code,
                            simulated_intent_id=pending_intent.id,
                            status="active",
                            expires_at=func.now() + text(f"interval '{duration_hours} hours'"),
                        )
                        .returning(SimulatedUserPass.id)
                    )
                    pass_row = pass_result.first()
                    try:
                        await conn.execute(
                            insert(FunnelEvent).values(
                                event_name="simulated_pass_activated",
                                event_source="api",
                                event_version=1,
                                anon_id=getattr(request.state, "anon_id", None),
                                session_id=request.cookies.get(settings.SESSION_COOKIE_NAME),
                                user_id=user_row.id,
                                effective_tier="simulated_paid",
                                selected_language=request.cookies.get("dd_lang") or "en",
                                target_tier="simulated_paid",
                                transition_name="free_to_simulated_paid",
                                related_simulated_intent_id=pending_intent.id,
                                related_simulated_pass_id=(pass_row.id if pass_row else None),
                                ui_surface="user_access_modal",
                                metadata_json={"plan_code": pending_intent.plan_code},
                            )
                        )
                    except Exception as event_err:
                        _report_funnel_failure(
                            route="/api/auth/magic",
                            event_name="simulated_pass_activated",
                            request=request,
                            exc=event_err,
                        )
        except Exception as e:
            logger.error(f"AUTH_MAGIC_DB_ACTIVATION_FAILED: {e}")
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
