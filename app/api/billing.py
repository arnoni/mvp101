import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from app.core.config import settings
from app.schemas.billing import UnlockIntentRequest, UnlockIntentResponse
from app.services.plan_catalog_service import get_plan_by_code
from app.utils.security import get_client_ip, protect_mutation, verify_turnstile
from app.utils.url import resolve_checkout_base

router = APIRouter()


async def _create_dodo_checkout(
    *,
    email: str,
    plan_code: str,
    amount_usd_cents: int,
    currency: str,
    intent_id: str,
) -> tuple[str, str | None]:
    if not settings.DODO_API_KEY:
        raise HTTPException(status_code=503, detail="Dodo API key is not configured")

    base_url = "https://api.dodopayments.com/v1" if settings.ENV == "production" else "https://test.dodopayments.com/v1"
    app_origin = resolve_checkout_base(settings.APP_ORIGIN).rstrip("/")

    payload = {
        "customer": {"email": email},
        "amount": amount_usd_cents,
        "currency": currency,
        "metadata": {
            "plan": plan_code,
            "email": email,
            "intent_id": intent_id,
        },
        "success_url": f"{app_origin}/?payment=success",
        "cancel_url": f"{app_origin}/?payment=cancelled",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"{base_url}/checkouts",
            headers={
                "Authorization": f"Bearer {settings.DODO_API_KEY}",
                "Content-Type": "application/json",
                "Idempotency-Key": intent_id,
            },
            json=payload,
        )
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="Failed to create Dodo checkout session")
        data = response.json()

    checkout_url = data.get("checkout_url") or data.get("url") or data.get("data", {}).get("checkout_url")
    checkout_id = data.get("checkout_id") or data.get("id") or data.get("data", {}).get("id")
    if not checkout_url:
        raise HTTPException(status_code=502, detail="Dodo checkout URL not present in response")
    return checkout_url, checkout_id


@router.post('/unlock-intent', response_model=UnlockIntentResponse)
async def unlock_intent(payload: UnlockIntentRequest, request: Request):
    await protect_mutation(request)
    if not payload.turnstile_token:
        raise HTTPException(status_code=400, detail="Turnstile token required")
    
    # Check for Smoke Test Bypass
    is_smoke_test = (
        settings.SMOKE_TURNSTILE_TOKEN 
        and payload.turnstile_token == settings.SMOKE_TURNSTILE_TOKEN
    )
    is_test_account = payload.email.lower() == "dilldrillteam@gmail.com"
    
    is_valid_turnstile = await verify_turnstile(payload.turnstile_token, client_ip=get_client_ip(request))
    if not is_valid_turnstile:
        raise HTTPException(status_code=403, detail="Turnstile verification failed")

    db_engine = getattr(request.app.state, "db_engine", None)
    if not db_engine:
        raise HTTPException(status_code=503, detail="Database is not configured")

    intent_id = str(uuid.uuid4())
    try:
        async with db_engine.begin() as conn:
            user_row = await conn.execute(
                text(
                    """
                    INSERT INTO users (email, ab_cohort)
                    VALUES (:email, CASE WHEN random() < 0.5 THEN 'A' ELSE 'B' END)
                    ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email
                    RETURNING id, ab_cohort
                    """
                ),
                {"email": payload.email.lower()},
            )
            user_record = user_row.mappings().first()
            user_id = user_record["id"]
            ab_cohort = user_record["ab_cohort"]
            
            resolved_plan_code = f"{payload.plan}_test_{ab_cohort.lower()}"
            
            plan_result = await conn.execute(
                text(
                    """
                    SELECT code, amount_usd_cents, currency, dodo_product_id, display_price
                    FROM billing_plans
                    WHERE code = :code AND is_active = true
                    LIMIT 1
                    """
                ),
                {"code": resolved_plan_code},
            )
            plan_row = plan_result.mappings().first()
            if not plan_row:
                raise HTTPException(status_code=400, detail="Invalid or inactive plan")
                
            from app.services.plan_catalog_service import PlanConfig
            plan = PlanConfig(
                code=plan_row["code"],
                amount_usd_cents=int(plan_row["amount_usd_cents"]),
                currency=plan_row["currency"],
                dodo_product_id=plan_row.get("dodo_product_id"),
                display_price=plan_row["display_price"],
            )

            provider_intent_id = intent_id
            if is_smoke_test or is_test_account:
                provider_intent_id = f"smoke_intent_{uuid.uuid4().hex[:8]}"

            await conn.execute(
                text(
                    """
                    INSERT INTO payment_intents (id, user_id, plan_code, amount_cents, provider_intent_id, currency, status)
                    VALUES (:id, :user_id, :plan_code, :amount_cents, :provider_intent_id, :currency, :status)
                    """
                ),
                {
                    "id": intent_id,
                    "user_id": user_id,
                    "plan_code": plan.code,
                    "amount_cents": plan.amount_usd_cents,
                    "provider_intent_id": provider_intent_id,
                    "currency": plan.currency,
                    "status": "pending" if (is_smoke_test or is_test_account) else "initiated"
                },
            )

        if is_smoke_test or is_test_account:
            logger.info(f"🧪 [dilldrillteamtest] Detected test account or smoke test: {payload.email}")
            # SMOKE/TEST BYPASS: Skip Dodo API network call
            app_origin = resolve_checkout_base(settings.APP_ORIGIN).rstrip("/")
            
            # --- Trigger Magic Link for Test Account ---
            if is_test_account:
                logger.info("🧪 [dilldrillteamtest] Sending magic link automatically for test account.")
                try:
                    # We can't easily call the login route directly, but we can 
                    # use the same logic here to send the link.
                    raw_token = secrets.token_urlsafe(32)
                    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
                    redis_payload = json.dumps({"user_id": str(user_id)})
                    
                    redis_cli = getattr(request.app.state, "redis", None)
                    if redis_cli:
                        await redis_cli.set(f"magic:{token_hash}", redis_payload, ex=1800)
                        logger.info(f"DEBUG: Magic link token stored in Redis for {payload.email}")
                        
                        from email_service import EmailService
                        email_service = EmailService()
                        magic_url = f"{app_origin}/api/auth/magic?token={raw_token}"
                        await email_service.send_magic_link(
                            email=payload.email.lower(),
                            magic_link=magic_url,
                            expire_minutes=30
                        )
                        logger.info(f"DEBUG: Magic link email sent to {payload.email}")
                except Exception as e:
                    logger.error(f"ERROR: Failed to send auto-magic link for test account: {e}")
            
            mock_checkout_url = f"{app_origin}/?payment=success" if is_test_account else "https://dodo.mock/checkout"
            return UnlockIntentResponse(
                checkout_url=mock_checkout_url,
                intent_id=intent_id
            )

        checkout_url, checkout_id = await _create_dodo_checkout(
            email=payload.email.lower(),
            plan_code=plan.code,
            amount_usd_cents=plan.amount_usd_cents,
            currency=plan.currency,
            intent_id=intent_id,
        )
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE payment_intents
                    SET status = 'pending', provider_intent_id = :provider_intent_id, updated_at = NOW()
                    WHERE id = :id
                    """
                ),
                {
                    "id": intent_id,
                    "provider_intent_id": checkout_id or intent_id,
                },
            )
    except Exception:
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    UPDATE payment_intents
                    SET status = 'api_error', updated_at = NOW()
                    WHERE id = :id AND status = 'initiated'
                    """
                ),
                {"id": intent_id},
            )
        raise

    return UnlockIntentResponse(checkout_url=checkout_url, intent_id=intent_id)
