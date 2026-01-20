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

    def find_nearest_pois(self, user_lat: float, user_lon: float, max_results: int = 5) -> List[PublicPOIResult]:
        """
        Calculate distances to all POIs and return the nearest results 
        that satisfy the 30m minimum spacing rule.
        
        Args:
            user_lat (float): User's latitude.
            user_lon (float): User's longitude.
            max_results (int): Maximum number of results to return (Tier dependent).
            
        Returns:
            List[PublicPOIResult]: Filtered list of POIs.
        """
        if not self.master_list:
            return []

        # 1. Gather all candidates within 100m (0.1 km)
        search_radius_km = 0.1 # Fixed TDD requirement
        candidates: List[Tuple[float, POI]] = []
        
        for poi in self.master_list:
            physical_dist = haversine(user_lat, user_lon, poi.lat, poi.lon)
            if physical_dist <= search_radius_km:
                candidates.append((physical_dist, poi))

        # 2. Sort by distance from user (closest first)
        candidates.sort(key=lambda x: x[0])
        
        # 3. Greedy Selection with 30m Spacing
        selected_tuples: List[Tuple[float, POI]] = []
        min_spacing_km = 0.03 # 30 meters
        
        for dist_from_user, candidate_poi in candidates:
            if len(selected_tuples) >= max_results:
                break
                
            # Check spacing against ALL already selected POIs
            is_far_enough = True
            for _, selected_poi in selected_tuples:
                inter_poi_dist = haversine(
                    candidate_poi.lat, candidate_poi.lon, 
                    selected_poi.lat, selected_poi.lon
                )
                if inter_poi_dist < min_spacing_km:
                    is_far_enough = False
                    break
            
            if is_far_enough:
                selected_tuples.append((dist_from_user, candidate_poi))

        # 4. Build DTOs for the response
        results: List[PublicPOIResult] = []
        for dist_km, poi in selected_tuples:
            google_maps_link = (
                f"https://www.google.com/maps/dir/?api=1&destination={poi.lat},{poi.lon}"
            )
            image_url = f"/static/images/{poi.images[0]}" if poi.images else ""

            results.append(
                PublicPOIResult(
                    name=poi.name,
                    distance_km=round(dist_km, 3), # Higher precision for meters check
                    google_maps_link=google_maps_link,
                    image_url=image_url,
                    lat=poi.lat,
                    lon=poi.lon,
                )
            )
        return results
