# app/services/poi_service.py
# Implements TSD Section 4.1: Repository Pattern for MasterList (in‑memory cache on startup)
# Implements TSD FR-005: Find Nearest 5 POIs
# Implements TSD Section 8: Never expose internal_notes or full MasterList

import json
import logging
import os
from typing import List, Tuple

from app.models.dto import POI, PublicPOIResult, MasterList
from app.utils.haversine import haversine
from app.core.config import settings

logger = logging.getLogger(__name__)

class POIService:
    """Service layer for handling Point‑of‑Interest (POI) data.

    - Loads `MasterList.json` into memory on startup.
    - Provides `find_nearest_pois` to return the 5 closest POIs.
    """

    def __init__(self):
        self.master_list: List[POI] = []
        self._load_master_list()

    def _load_master_list(self):
        """Load the MasterList JSON file and validate it with the Pydantic model."""
        file_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "static", "masterlist.json"
        )
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Validate using the MasterList DTO (which contains a list of POI objects)
            master = MasterList.model_validate(data)
            self.master_list = master.points
            logger.info(
                f"Successfully loaded {len(self.master_list)} POIs from MasterList."
            )
        except FileNotFoundError:
            logger.error(f"MasterList file not found at: {file_path}")
            self.master_list = []
        except Exception as e:
            logger.error(f"Error loading or validating MasterList: {e}")
            self.master_list = []

    async def initialize_semantic_search(self):
        """Deprecated: Semantic search removed in favor of direct coordinate support."""
        pass

    def find_nearest_pois(self, user_lat: float, user_lon: float) -> List[PublicPOIResult]:
        """Calculate distances to all POIs and return the nearest five within the configured search radius."""
        if not self.master_list:
            return []

        # Haversine against all points (in‑memory list)
        distances: List[Tuple[float, POI]] = []
        for poi in self.master_list:
            # Physical Distance (Real World)
            physical_dist = haversine(user_lat, user_lon, poi.lat, poi.lon)
            
            # Simple check against configured radius
            if physical_dist <= settings.SEARCH_RADIUS_KM:
                distances.append((physical_dist, poi))

        # Sort by distance and take the top 5
        nearest_pois = sorted(distances, key=lambda x: x[0])[:5]

        # Build DTOs for the response
        results: List[PublicPOIResult] = []
        for dist_km, poi in nearest_pois:
            google_maps_link = (
                f"https://www.google.com/maps/dir/?api=1&destination={poi.lat},{poi.lon}"
            )
            image_url = f"/static/images/{poi.images[0]}" if poi.images else ""

            results.append(
                PublicPOIResult(
                    name=poi.name,
                    distance_km=round(dist_km, 2),
                    google_maps_link=google_maps_link,
                    image_url=image_url,
                    lat=poi.lat,
                    lon=poi.lon,
                )
            )
        return results
