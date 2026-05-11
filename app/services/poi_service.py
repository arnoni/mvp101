# app/services/poi_service.py
import time
from typing import Any, Dict, List, Tuple

import sentry_sdk
import structlog
from pydantic import validate_call
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.analytics import capture

logger = structlog.get_logger(__name__)

ZERO_DISTANCE_BINS = {"0_10": 0, "10_20": 0, "20_30": 0, "30_40": 0}


class POIService:
    def __init__(self, engine: AsyncEngine | None):
        self.engine = engine
        self.master_list = []

    # MVP102: This service is largely deprecated in favor of PrecomputeRepository.
    # We keep it compilable for now to avoiding breaking strict dependency injection checks if any remain.
    @validate_call
    async def find_nearest_pois(
        self,
        user_lat: float,
        user_lon: float,
        max_results: int = 5,
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        logger.warning(
            "deprecated_method_called",
            method="find_nearest_pois",
            user_lat=round(user_lat, 5),
            user_lon=round(user_lon, 5),
            max_results=max_results,
        )
        sentry_sdk.capture_message("Deprecated find_nearest_pois called", level="info")
        return [], []

    @validate_call
    async def get_pois_by_names(self, names: List[str]) -> List[Dict[str, Any]]:
        logger.warning("deprecated_method_called", method="get_pois_by_names", name_count=len(names))
        sentry_sdk.capture_message("Deprecated get_pois_by_names called", level="info")
        return []

    @validate_call
    async def get_construction_distance_bins(
        self,
        user_lat: float,
        user_lon: float,
    ) -> Dict[str, int]:
        """Query raw `pois` and return COUNT(*) per km-distance tier from the user's GPS."""
        start_ms = time.time()
        zero_bins = ZERO_DISTANCE_BINS.copy()
        if not self.engine:
            duration_ms = round((time.time() - start_ms) * 1000, 1)
            logger.warning(
                "poi_distance_bin_query_skipped",
                reason="engine_unavailable",
                user_lat=round(user_lat, 5),
                user_lon=round(user_lon, 5),
                duration_ms=duration_ms,
            )
            return zero_bins

        stmt = text(
            """
            WITH origin AS (
                SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography AS g
            ),
            nearby AS (
                SELECT ST_Distance(p.geom, o.g) AS dist_m
                FROM pois p
                CROSS JOIN origin o
                WHERE ST_DWithin(p.geom, o.g, 40000)
            )
            SELECT
                COUNT(*) FILTER (WHERE dist_m >=     0 AND dist_m < 10000) AS c0_10,
                COUNT(*) FILTER (WHERE dist_m >= 10000 AND dist_m < 20000) AS c10_20,
                COUNT(*) FILTER (WHERE dist_m >= 20000 AND dist_m < 30000) AS c20_30,
                COUNT(*) FILTER (WHERE dist_m >= 30000 AND dist_m < 40000) AS c30_40
            FROM nearby
            """
        )

        try:
            async with self.engine.connect() as conn:
                query_result = await conn.execute(stmt, {"lat": user_lat, "lon": user_lon})
                row = query_result.mappings().first()
            result = zero_bins if not row else {
                "0_10": int(row.get("c0_10") or 0),
                "10_20": int(row.get("c10_20") or 0),
                "20_30": int(row.get("c20_30") or 0),
                "30_40": int(row.get("c30_40") or 0),
            }
            duration_ms = round((time.time() - start_ms) * 1000, 1)
            logger.info(
                "distance_bins_queried",
                user_lat=round(user_lat, 5),
                user_lon=round(user_lon, 5),
                tier_counts=result,
                duration_ms=duration_ms,
            )
            capture(
                user_id="system",
                event="distance_bins_queried",
                properties={
                    "tier_counts": result,
                    "lat": round(user_lat, 5),
                    "lon": round(user_lon, 5),
                    "duration_ms": duration_ms,
                },
            )
            return result
        except Exception as exc:
            duration_ms = round((time.time() - start_ms) * 1000, 1)
            logger.error(
                "poi_distance_bin_query_failed",
                function="get_construction_distance_bins",
                user_lat=round(user_lat, 5),
                user_lon=round(user_lon, 5),
                error_type=type(exc).__name__,
                error_detail=str(exc),
                duration_ms=duration_ms,
            )
            sentry_sdk.capture_exception(exc)
            capture(
                user_id="system",
                event="distance_bins_failed",
                properties={
                    "error_type": type(exc).__name__,
                    "lat": round(user_lat, 5),
                    "lon": round(user_lon, 5),
                    "duration_ms": duration_ms,
                },
            )
            return zero_bins
