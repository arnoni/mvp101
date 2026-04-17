from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from upstash_redis.asyncio import Redis

from app.services.location_parser import ParsedLocationInput


@dataclass(frozen=True)
class QueryHistoryEvent:
    parsed: ParsedLocationInput
    anon_id: str | None
    session_id: str | None
    user_id: int | None
    demand_cell_id: str | None
    user_agent: str | None
    request_country: str | None
    request_city: str | None
    result_status: str
    result_count: int | None
    error_code: str | None
    response_ms: int | None


class QueryHistoryRepository:
    def __init__(self, db_engine: Optional[AsyncEngine], redis: Optional[Redis] = None):
        self.db_engine = db_engine
        self.redis = redis

    async def _is_duplicate_window(self, event: QueryHistoryEvent, window_seconds: int = 60) -> bool:
        if not self.redis:
            return False
        identity = event.user_id or event.session_id or event.anon_id or "unknown"
        dedupe_cell = event.demand_cell_id or f"{event.parsed.latitude:.3f},{event.parsed.longitude:.3f}"
        bucket = int(time() // window_seconds)
        dedupe_key = f"dd:history_dedupe:{identity}:{dedupe_cell}:{bucket}"
        claimed = await self.redis.set(dedupe_key, "1", ex=window_seconds, nx=True)
        return not bool(claimed)

    async def log_event(self, event: QueryHistoryEvent) -> int | None:
        if not self.db_engine:
            return None

        is_dup = await self._is_duplicate_window(event)
        stmt = text(
            """
            INSERT INTO user_location_queries (
              anon_id, session_id, user_id, input_kind, original_input, source_url,
              normalized_input, latitude, longitude, location, demand_cell_id,
              request_country, request_city, user_agent, result_status, result_count,
              error_code, response_ms, is_duplicate_window
            )
            VALUES (
              :anon_id, :session_id, :user_id, :input_kind, :original_input, :source_url,
              :normalized_input, :latitude, :longitude,
              ST_SetSRID(ST_MakePoint(:longitude, :latitude), 4326)::geography,
              :demand_cell_id, :request_country, :request_city, :user_agent,
              :result_status, :result_count, :error_code, :response_ms, :is_duplicate_window
            )
            RETURNING id
            """
        )

        async with self.db_engine.begin() as conn:
            result = await conn.execute(
                stmt,
                {
                    "anon_id": event.anon_id,
                    "session_id": event.session_id,
                    "user_id": event.user_id,
                    "input_kind": event.parsed.input_kind,
                    "original_input": event.parsed.original_input,
                    "source_url": event.parsed.source_url,
                    "normalized_input": event.parsed.normalized_input,
                    "latitude": event.parsed.latitude,
                    "longitude": event.parsed.longitude,
                    "demand_cell_id": event.demand_cell_id,
                    "request_country": event.request_country,
                    "request_city": event.request_city,
                    "user_agent": event.user_agent,
                    "result_status": event.result_status,
                    "result_count": event.result_count,
                    "error_code": event.error_code,
                    "response_ms": event.response_ms,
                    "is_duplicate_window": is_dup,
                },
            )
            row = result.first()
            return int(row.id) if row else None
