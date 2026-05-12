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
    ) -> tuple[Dict[str, int], float]:
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
            return zero_bins, float("inf")

        stmt = text(
            """
            WITH origin AS (
                SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography AS g
            )
            SELECT ST_Distance(p.geom, o.g) AS dist_m
            FROM pois p
            CROSS JOIN origin o
            WHERE ST_DWithin(p.geom, o.g, 40000)
            """
        )

        try:
            async with self.engine.connect() as conn:
                query_result = await conn.execute(stmt, {"lat": user_lat, "lon": user_lon})
                rows = query_result.mappings().all()

            raw_sample = [
                {"dist_m": round(float(row.get("dist_m")), 2)}
                for row in rows[:5]
                if row.get("dist_m") is not None
            ]
            logger.debug(
                "distance_bins_raw_result",
                user_lat=user_lat,
                user_lon=user_lon,
                raw_row_count=len(rows),
                raw_sample=raw_sample,
            )

            result = zero_bins.copy()
            min_distance_m = float("inf")
            for row in rows:
                dist_m_value = row.get("dist_m")
                if dist_m_value is None:
                    continue
                dist_m = float(dist_m_value)
                min_distance_m = min(min_distance_m, dist_m)
                if 0 <= dist_m < 10:
                    result["0_10"] += 1
                elif 10 <= dist_m < 20:
                    result["10_20"] += 1
                elif 20 <= dist_m < 30:
                    result["20_30"] += 1
                elif 30 <= dist_m < 40:
                    result["30_40"] += 1

            total = sum(result.values())
            if total > 20:
                logger.warning(
                    "distance_bins_anomaly",
                    total_pois=total,
                    bins=result,
                    user_lat=user_lat,
                    user_lon=user_lon,
                    detail="Bin count far exceeds expected POI density. "
                    "Likely SQL duplication, bad GROUP BY, or missing parameter binding.",
                )

            duration_ms = round((time.time() - start_ms) * 1000, 1)
            logger.info(
                "distance_bins_queried",
                user_lat=round(user_lat, 5),
                user_lon=round(user_lon, 5),
                tier_counts=result,
                min_dist_m=None if min_distance_m == float("inf") else round(min_distance_m, 2),
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
                    "min_dist_m": None if min_distance_m == float("inf") else round(min_distance_m, 2),
                },
            )
            return result, min_distance_m
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
            return zero_bins, float("inf")
