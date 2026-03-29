from typing import Optional

from pydantic import BaseModel, EmailStr


class UnlockIntentRequest(BaseModel):
    email: EmailStr
    plan: str
    turnstile_token: Optional[str] = None


class UnlockIntentResponse(BaseModel):
    checkout_url: str
    message: str = "Checkout initialized"
