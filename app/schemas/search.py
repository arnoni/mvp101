from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class SearchTarget(str, Enum):
    CONSTRUCTION = "construction"
    DEMAND = "demand"
    BOTH = "both"


class SearchRequest(BaseModel):
    lat: Optional[float] = None
    lon: Optional[float] = None
    location_input: Optional[str] = Field(default=None, max_length=2048)
    target: SearchTarget = SearchTarget.BOTH
    turnstile_token: Optional[str] = None

    @model_validator(mode="after")
    def validate_location(self):
        if self.location_input:
            return self
        if self.lat is None or self.lon is None:
            raise ValueError("Either location_input or lat/lon is required")
        return self


class GaugeResult(BaseModel):
    score: Optional[int] = None
    coord_key: Optional[str] = None
    message_code: Optional[str] = None
    message: Optional[str] = None
    cached: bool = False


class SearchResponse(BaseModel):
    construction: Optional[GaugeResult] = None
    demand: Optional[GaugeResult] = None
    message_code: Optional[str] = None
    message: Optional[str] = None
    quota_remaining: int
    checks_today: int
    tier: Optional[str] = None
