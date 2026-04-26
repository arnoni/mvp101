from enum import Enum
from typing import Optional

from pydantic import BaseModel, EmailStr


class UnlockUiSurface(str, Enum):
    CONSTRUCTION_LEVEL_PAGE = "construction_level_page"
    DEMAND_LEVEL_PAGE = "demand_level_page"
    HERO_UNLOCK_BUTTON = "hero_unlock_button"
    USER_ACCESS_MODAL = "user_access_modal"
    SHARE_MODAL = "share_modal"
    ABOUT_MODAL = "about_modal"


class UnlockIntentRequest(BaseModel):
    email: EmailStr
    plan: str
    turnstile_token: Optional[str] = None
    ui_surface: Optional[UnlockUiSurface] = None


class UnlockIntentResponse(BaseModel):
    ok: bool = True
    status: str = "magic_link_sent"
    intent_id: str
    message: str = "If this email is eligible, we sent a new access link."
    checkout_url: str | None = None
