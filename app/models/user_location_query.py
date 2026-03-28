from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserLocationQuery(Base):
    __tablename__ = "user_location_queries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    anon_id: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int | None] = mapped_column(Integer)
    input_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    original_input: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    normalized_input: Mapped[str] = mapped_column(Text, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    demand_cell_id: Mapped[str | None] = mapped_column(String(64))
    request_country: Mapped[str | None] = mapped_column(String(120))
    request_city: Mapped[str | None] = mapped_column(String(120))
    user_agent: Mapped[str | None] = mapped_column(Text)
    result_status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_count: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    response_ms: Mapped[int | None] = mapped_column(Integer)
    is_duplicate_window: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
