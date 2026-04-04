import hashlib
import hmac
import json
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text

from app.core.config import settings
from app.services.entitlement_service import EntitlementService, TierStatus
from email_service import EmailService

router = APIRouter()
logger = logging.getLogger(__name__)


def verify_dodo_signature(raw_body: bytes, signature: str | None) -> bool:
    """Verify Dodo webhook signature (HMAC SHA-256)."""
    if settings.ENV == "development" and not settings.DODO_WEBHOOK_SECRET:
        return True
    if not signature or not settings.DODO_WEBHOOK_SECRET:
        return False
    expected = hmac.new(settings.DODO_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    supplied = signature.replace("sha256=", "").strip()
    return hmac.compare_digest(expected, supplied)


def parse_dodo_event(raw_body: bytes):
    try:
        data = json.loads(raw_body)
        return type(
            "Event",
            (),
            {
                "id": data.get("payment_id"),
                "email": data.get("customer", {}).get("email"),
                "intent_id": data.get("metadata", {}).get("intent_id"),
                "amount_cents": (
                    data.get("amount")
                    or data.get("amount_minor")
                    or data.get("total_amount")
                    or data.get("data", {}).get("amount")
                ),
                "currency": (
                    data.get("currency")
                    or data.get("currency_code")
                    or data.get("data", {}).get("currency")
                    or "USD"
                ),
                "status": data.get("status"),
            },
        )
    except (json.JSONDecodeError, AttributeError, KeyError) as e:
        logger.error(f"WEBHOOK_DODO_PARSE_FAILED: {e}")
        return None


async def get_services(request: Request):
    return {"db": request.app.state.db_engine, "redis": request.app.state.redis, "email": EmailService()}


@router.post("/dodo")
async def dodo_webhook(request: Request, services: dict = Depends(get_services)):
    db = services["db"]
    redis = services["redis"]
    email_service = services["email"]

    try:
        raw_body = await request.body()
        signature = request.headers.get("Dodo-Signature")
        if not verify_dodo_signature(raw_body, signature):
            logger.warning("WEBHOOK_DODO_INVALID_SIGNATURE")
            raise HTTPException(status_code=401, detail="Invalid signature")

        event = parse_dodo_event(raw_body)
        if not event or not event.id or not event.email:
            raise HTTPException(status_code=400, detail="Incomplete event data")

        lock_key = f"lock:webhook:{event.id}"
        acquired = await redis.set(lock_key, "1", nx=True, ex=30)
        if not acquired:
            raise HTTPException(status_code=409, detail="Concurrent processing")

        try:
            async with db.begin() as conn:
                already = await conn.execute(text("SELECT 1 FROM webhook_events WHERE event_id = :eid"), {"eid": event.id})
                if already.fetchone():
                    return {"status": "ok"}

                if not event.intent_id:
                    return {"status": "ignored", "reason": "missing_intent"}

                intent_res = await conn.execute(
                    text(
                        """
                        SELECT id, user_id, plan_code, amount_cents, currency, status
                        FROM payment_intents
                        WHERE id = :intent_id
                        LIMIT 1
                        FOR UPDATE
                        """
                    ),
                    {"intent_id": event.intent_id},
                )
                intent = intent_res.mappings().first()
                if not intent:
                    return {"status": "ignored", "reason": "unknown_intent"}
                if str(intent["status"]).lower() == "paid":
                    return {"status": "ok"}

                webhook_amount = int(float(str(event.amount_cents)) * 100) if "." in str(event.amount_cents) else int(event.amount_cents)
                webhook_currency = str(event.currency or "USD").upper()
                if webhook_amount != int(intent["amount_cents"]) or webhook_currency != str(intent["currency"]).upper():
                    return {"status": "ignored", "reason": "amount_currency_mismatch"}

                duration_res = await conn.execute(
                    text("SELECT duration_days, daily_limit FROM billing_plans WHERE code = :code LIMIT 1"),
                    {"code": intent["plan_code"]},
                )
                duration_row = duration_res.mappings().first()
                if not duration_row:
                    return {"status": "ignored", "reason": "unknown_plan"}
                duration_days = int(duration_row["duration_days"])
                daily_limit = int(duration_row["daily_limit"])

                issued_pass_res = await conn.execute(
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
                            :amount_paid_cents,
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
                        RETURNING id, plan_code, user_id, expires_at
                        """
                    ),
                    {
                        "user_id": intent["user_id"],
                        "plan_code": intent["plan_code"],
                        "provider_payment_id": event.id,
                        "amount_paid_cents": int(intent["amount_cents"]),
                        "duration_days": duration_days,
                    },
                )
                issued_pass = issued_pass_res.mappings().first()

                await conn.execute(
                    text(
                        """
                        UPDATE payment_intents
                        SET status = 'paid', provider_intent_id = :provider_intent_id, updated_at = NOW()
                        WHERE id = :intent_id
                        """
                    ),
                    {"provider_intent_id": event.id, "intent_id": event.intent_id},
                )
                await conn.execute(
                    text("INSERT INTO webhook_events (provider, event_id, payload) VALUES ('dodo', :eid, :payload)"),
                    {
                        "eid": event.id,
                        "payload": raw_body.decode("utf-8")
                    }
                )

                tier = TierStatus.PASS_3_DAY if duration_days >= 3 else TierStatus.PASS_1_DAY
                expires_at_ts = int(issued_pass["expires_at"].timestamp()) if issued_pass.get("expires_at") else None
                await EntitlementService.cache_entitlement(
                    user_id=str(issued_pass["user_id"]),
                    tier=tier,
                    redis_cli=redis,
                    active_plan_code=str(issued_pass["plan_code"]),
                    daily_limit=daily_limit,
                    expires_at=expires_at_ts,
                )

            raw_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
            redis_payload = json.dumps(
                {
                    "user_id": str(issued_pass["user_id"]),
                }
            )
            print(f"About to call redis.set() for magic link: {token_hash[:8]}")
            import asyncio
            await asyncio.wait_for(
                redis.set(f"magic:{token_hash}", redis_payload, ex=1800),
                timeout=10,
            )
            print(f"redis.set() finished for magic link: {token_hash[:8]}")
            app_origin = settings.APP_ORIGIN or "http://localhost:8000"
            magic_url = f"{app_origin}/api/auth/magic?token={raw_token}"
            await email_service.send_magic_link(email=event.email, magic_link=magic_url, expire_minutes=30)
        finally:
            await redis.delete(lock_key)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"WEBHOOK_DODO_CRITICAL_FAILURE: {e}")
        raise HTTPException(status_code=500, detail="Critical webhook failure")

    return {"status": "ok"}
