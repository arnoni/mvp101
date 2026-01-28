# app/services/poi_service.py
from typing import List, Tuple
import structlog
from sqlalchemy import text, bindparam
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import Text
from sqlalchemy.ext.asyncio import AsyncEngine
from app.models.dto import PublicPOIResult, PublicPOIResultWithCoords
from app.utils.haversine import haversine
from app.core.config import settings

logger = structlog.get_logger(__name__)

class POIService:
    def __init__(self, engine: AsyncEngine | None):
        self.engine = engine
        self.master_list = []

    async def find_nearest_pois(self, user_lat: float, user_lon: float, max_results: int = 5, include_coords: bool = False) -> Tuple[List[PublicPOIResult], List[str]]:
        logs: List[str] = []
        if not self.engine or not settings.DATABASE_URL:
            logs.append("DATABASE_URL is not configured.")
            return [], logs

        radius_m = int(settings.SEARCH_RADIUS_KM * 1000)
        logs.append(f"Searching near {user_lat:.5f}, {user_lon:.5f} within {radius_m}m via PostGIS")

        sql = text(
            "SELECT name, "
            "ST_Distance(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::GEOGRAPHY) AS distance_m, "
            "ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon "
            "FROM pois "
            "WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::GEOGRAPHY, :radius) "
            "ORDER BY distance_m LIMIT :limit"
        )

        # Query more candidates than needed to allow spacing filter
        initial_limit = max(max_results * 5, 25)

        candidates: List[Tuple[float, Tuple[str, float, float]]] = []
        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(
                    sql.bindparams(lon=user_lon, lat=user_lat, radius=radius_m, limit=initial_limit)
                )
                rows = result.fetchall()
                for row in rows:
                    name, distance_m, lat, lon = row
                    candidates.append((distance_m / 1000.0, (name, lat, lon)))
                    logs.append(f"Candidate: {name} at {distance_m:.1f}m")
        except Exception as e:
            logs.append(f"DB query failed: {e}")
            logger.error("postgis_query_failed", error=str(e))
            return [], logs

        if not candidates:
            logs.append("No candidates found within search radius.")
            return [], logs

        selected: List[Tuple[float, Tuple[str, float, float]]] = []
        min_spacing_km = 0.03
        for dist_km, (name, lat, lon) in candidates:
            if len(selected) >= max_results:
                break
            is_far_enough = True
            for _, (_, s_lat, s_lon) in selected:
                inter_poi_dist_km = haversine(lat, lon, s_lat, s_lon)
                if inter_poi_dist_km < min_spacing_km:
                    is_far_enough = False
                    logs.append(f"Skip {name}: too close ({inter_poi_dist_km*1000:.1f}m)")
                    break
            if is_far_enough:
                selected.append((dist_km, (name, lat, lon)))
                logs.append(f"Selected {name} (Dist: {dist_km*1000:.1f}m)")

        results: List[PublicPOIResult] = []
        for dist_km, (name, lat, lon) in selected:
            google_maps_link = f"https://www.google.com/maps/dir/?api=1&origin={user_lat},{user_lon}&destination={lat},{lon}"
            distance_m = int(round(dist_km * 1000))
            if include_coords:
                results.append(
                    PublicPOIResultWithCoords(
                        name=name,
                        distance_m=distance_m,
                        google_maps_link=google_maps_link,
                        image_url=None,
                        lat=lat,
                        lon=lon,
                    )
                )
            else:
                results.append(
                    PublicPOIResult(
                        name=name,
                        distance_m=distance_m,
                        google_maps_link=google_maps_link,
                        image_url=None,
                    )
                )
        return results, logs

    async def get_pois_by_names(self, names: List[str], include_coords: bool = True) -> List[PublicPOIResult]:
        if not self.engine or not settings.DATABASE_URL or not names:
            return []
        sql = (
            text(
                "SELECT name, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon "
                "FROM pois WHERE name = ANY(:names)"
            )
            .bindparams(bindparam("names", type_=ARRAY(Text)))
        )
        results: List[PublicPOIResult] = []
        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(sql, {"names": names})
                rows = result.fetchall()
                for row in rows:
                    name, lat, lon = row
                    gmaps = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"
                    if include_coords:
                        results.append(
                            PublicPOIResultWithCoords(
                                name=name,
                                distance_m=0,
                                google_maps_link=gmaps,
                                image_url=None,
                                lat=lat,
                                lon=lon,
                            )
                        )
                    else:
                        results.append(
                            PublicPOIResult(
                                name=name,
                                distance_m=0,
                                google_maps_link=gmaps,
                                image_url=None,
                            )
                        )
        except Exception as e:
            logger.error("postgis_names_query_failed", error=str(e))
            return []
        return results
