from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class ReportKind(str, Enum):
    ACTIVE_CONSTRUCTION = "active_construction"
    MAYBE_CONSTRUCTION = "maybe_construction"
    CONSTRUCTION_ENDED = "construction_ended"


class UserReportRequest(BaseModel):
    lat: float
    lon: float
    report_kind: ReportKind
    is_nearby_now: bool = False
    note: Optional[str] = Field(default="", max_length=180)
    cf_turnstile_token: Optional[str] = None
    location_source: Literal["hero_prefill", "manual_input"] | None = None


class UserReportResponse(BaseModel):
    ok: bool
    status: str = "report_created"
    report_id: str
    duplicate: bool = False
    message: str = "Report submitted. Thanks for helping others avoid noisy surprises."
