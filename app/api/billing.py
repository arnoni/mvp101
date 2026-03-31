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
    is_valid_turnstile = await verify_turnstile(payload.turnstile_token, client_ip=get_client_ip(request))
    if not is_valid_turnstile:
        raise HTTPException(status_code=403, detail="Turnstile verification failed")

    db_engine = getattr(request.app.state, "db_engine", None)
    if not db_engine:
        raise HTTPException(status_code=503, detail="Database is not configured")

    plan = await get_plan_by_code(db_engine, payload.plan)
    if not plan:
        raise HTTPException(status_code=400, detail="Invalid or inactive plan")

    intent_id = str(uuid.uuid4())
    try:
        async with db_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO payment_intents (id, email, plan_code, amount_usd_cents, currency, status)
                    VALUES (:id, :email, :plan_code, :amount_usd_cents, :currency, 'initiated')
                    """
                ),
                {
                    "id": intent_id,
                    "email": payload.email.lower(),
                    "plan_code": plan.code,
                    "amount_usd_cents": plan.amount_usd_cents,
                    "currency": plan.currency,
                },
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
                    SET status = 'pending', provider_event_id = :provider_event_id, updated_at = NOW()
                    WHERE id = :id
                    """
                ),
                {
                    "id": intent_id,
                    "provider_event_id": checkout_id,
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
