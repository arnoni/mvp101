# app/services/poi_service.py
import math
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
        """
        Query raw `pois` securely and return count bins in meters:
        [0,10), [10,20), [20,30), [30,40).
        """
        if not self.engine:
            return {"0_10": 0, "10_20": 0, "20_30": 0, "30_40": 0}

        # Bounding-box prefilter (~40m) before exact distance check (ST_DWithin).
        # 1 deg lat ~= 111_320m; lon adjusted by cos(lat).
        lat_deg_pad = 40.0 / 111_320.0
        lon_denominator = max(0.1, abs(math.cos(math.radians(user_lat))))
        lon_deg_pad = 40.0 / (111_320.0 * lon_denominator)

        stmt = text(
            """
            WITH origin AS (
                SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography AS g
            ),
            nearby AS (
                SELECT ST_Distance(p.geom, o.g) AS dist_m
                FROM pois p
                CROSS JOIN origin o
                WHERE ST_Intersects(
                    p.geom::geometry,
                    ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
                )
                AND ST_DWithin(p.geom, o.g, 40)
            )
            SELECT
                COUNT(*) FILTER (WHERE dist_m >= 0 AND dist_m < 10)  AS c0_10,
                COUNT(*) FILTER (WHERE dist_m >= 10 AND dist_m < 20) AS c10_20,
                COUNT(*) FILTER (WHERE dist_m >= 20 AND dist_m < 30) AS c20_30,
                COUNT(*) FILTER (WHERE dist_m >= 30 AND dist_m < 40) AS c30_40
            FROM nearby
            """
        )

        params = {
            "lat": user_lat,
            "lon": user_lon,
            "min_lat": user_lat - lat_deg_pad,
            "max_lat": user_lat + lat_deg_pad,
            "min_lon": user_lon - lon_deg_pad,
            "max_lon": user_lon + lon_deg_pad,
        }

        try:
            async with self.engine.connect() as conn:
                row = (await conn.execute(stmt, params)).mappings().first()
            if not row:
                return {"0_10": 0, "10_20": 0, "20_30": 0, "30_40": 0}
            return {
                "0_10": int(row.get("c0_10") or 0),
                "10_20": int(row.get("c10_20") or 0),
                "20_30": int(row.get("c20_30") or 0),
                "30_40": int(row.get("c30_40") or 0),
            }
        except Exception:
            logger.exception("poi_distance_bin_query_failed")
            return {"0_10": 0, "10_20": 0, "20_30": 0, "30_40": 0}
