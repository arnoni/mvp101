from enum import Enum, StrEnum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ReportKind(str, Enum):
    ACTIVE_CONSTRUCTION = "active_construction"
    MAYBE_CONSTRUCTION = "maybe_construction"
    CONSTRUCTION_ENDED = "construction_ended"


class ReportType(StrEnum):
    ACTIVE = "active"
    MAYBE = "maybe"
    ENDED = "ended"


REPORT_TYPE_TO_CATEGORY: dict[ReportType, str] = {
    ReportType.ACTIVE: "active_construction",
    ReportType.MAYBE: "maybe_construction",
    ReportType.ENDED: "construction_ended",
}

REPORT_TYPE_TO_SEVERITY: dict[ReportType, int] = {
    ReportType.ACTIVE: 5,
    ReportType.MAYBE: 3,
    ReportType.ENDED: 1,
}

REPORT_TYPE_TO_REPORT_KIND: dict[ReportType, ReportKind] = {
    ReportType.ACTIVE: ReportKind.ACTIVE_CONSTRUCTION,
    ReportType.MAYBE: ReportKind.MAYBE_CONSTRUCTION,
    ReportType.ENDED: ReportKind.CONSTRUCTION_ENDED,
}


class UserReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float
    lon: float
    report_type: ReportType
    is_nearby_now: bool = False
    note: Optional[str] = Field(default="", max_length=180)
    cf_turnstile_token: Optional[str] = None
    location_source: Literal["hero_prefill", "manual_input"] | None = None

    @property
    def report_kind(self) -> ReportKind:
        return REPORT_TYPE_TO_REPORT_KIND[self.report_type]


class UserReportResponse(BaseModel):
    ok: bool
    status: str = "report_created"
    report_id: str
    duplicate: bool = False
    message: str = "Report submitted. Thanks for helping others avoid noisy surprises."
