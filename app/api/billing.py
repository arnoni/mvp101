from fastapi import APIRouter

from app.core.config import settings
from app.schemas.billing import UnlockIntentRequest, UnlockIntentResponse

router = APIRouter()


@router.post('/unlock-intent', response_model=UnlockIntentResponse)
async def unlock_intent(payload: UnlockIntentRequest):
    base = settings.APP_ORIGIN or 'http://localhost:8000'
    checkout_url = f"{base}/checkout?plan={payload.plan}&email={payload.email}"
    return UnlockIntentResponse(checkout_url=checkout_url)
