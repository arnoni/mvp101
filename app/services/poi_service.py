# app/services/poi_service.py
from typing import List, Tuple, Dict, Any
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from pydantic import validate_call

logger = structlog.get_logger(__name__)

class POIService:
    def __init__(self, engine: AsyncEngine | None):
        self.engine = engine
        self.master_list = []

    # MVP102: This service is largely deprecated in favor of PrecomputeRepository.
    # We keep it compilable for now to avoiding breaking strict dependency injection checks if any remain.
    @validate_call
    async def find_nearest_pois(self, user_lat: float, user_lon: float, max_results: int = 5) -> Tuple[List[Dict[str, Any]], List[str]]:
        logs: List[str] = []
        # Return empty immediately as this path is legacy/unsafe
        logs.append("POIService.find_nearest_pois is deprecated/unsafe in MVP102.")
        return [], logs

    @validate_call
    async def get_pois_by_names(self, names: List[str]) -> List[Dict[str, Any]]:
        # Deprecated
        return []

    @validate_call
    async def get_construction_distance_bins(
        self,
        user_lat: float,
        user_lon: float,
    ) -> Dict[str, int]:
        """Query raw `pois` and return COUNT(*) per km-distance tier from the
        user's exact GPS coordinate.

        Tiers (kilometres from user coordinate):
            "0_10"  —  0 km <= d < 10 km
            "10_20" — 10 km <= d < 20 km
            "20_30" — 20 km <= d < 30 km
            "30_40" — 30 km <= d < 40 km

        Distance is computed as great-circle distance from the user's
        coordinate to each POI's geom using ST_DWithin on the existing
        geography column. The GiST index on geom is used natively for an
        index-driven distance scan — no bounding-box prefilter needed.

        Expected query plan: "Index Scan using idx_pois_geom on pois"
        (NOT "Seq Scan on pois").

        Returns zero counts if self.engine is None or the query fails.
        """
        zero_bins = {"0_10": 0, "10_20": 0, "20_30": 0, "30_40": 0}
        if not self.engine:
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
                row = (await conn.execute(stmt, {"lat": user_lat, "lon": user_lon})).mappings().first()
            if not row:
                return zero_bins
            return {
                "0_10": int(row.get("c0_10") or 0),
                "10_20": int(row.get("c10_20") or 0),
                "20_30": int(row.get("c20_30") or 0),
                "30_40": int(row.get("c30_40") or 0),
            }
        except Exception:
            logger.exception("poi_distance_bin_query_failed")
            return zero_bins
