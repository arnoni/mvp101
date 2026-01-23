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

import structlog

logger = structlog.get_logger(__name__)

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
            self.master_list = master.points
            logger.info("masterlist_loaded", count=len(self.master_list))
        except FileNotFoundError:
            logger.error("masterlist_file_not_found", path=file_path)
            self.master_list = []
        except Exception as e:
            logger.exception("masterlist_load_error", error=str(e))
            self.master_list = []

    async def initialize_semantic_search(self):
        """Deprecated: Semantic search removed in favor of direct coordinate support."""
        pass

    def find_nearest_pois(self, user_lat: float, user_lon: float, max_results: int = 5) -> Tuple[List[PublicPOIResult], List[str]]:
        """
        Calculate distances to all POIs and return the nearest results 
        that satisfy the 30m minimum spacing rule.
        
        Returns:
            Tuple[List[PublicPOIResult], List[str]]: (Filtered results, Debug logs)
        """
        logs: List[str] = []
        logs.append(f"Total POIs in MasterList: {len(self.master_list)}")

        if not self.master_list:
            logs.append("MasterList is empty.")
            return [], logs

        # 1. Gather all candidates within 100m (0.1 km)
        search_radius_km = 0.1 # Fixed TDD requirement
        candidates: List[Tuple[float, POI]] = []
        
        logs.append(f"Searching near {user_lat:.5f}, {user_lon:.5f} in radius {search_radius_km}km")
        
        for poi in self.master_list:
            physical_dist = haversine(user_lat, user_lon, poi.lat, poi.lon)
            if physical_dist <= search_radius_km:
                candidates.append((physical_dist, poi))
                logs.append(f"Candidate found: {poi.name} at {physical_dist*1000:.1f}m")

        if not candidates:
            logs.append("No candidates found within search radius.")
            return [], logs

        # 2. Sort by distance from user (closest first)
        logs.append(f"Sorting {len(candidates)} candidates by distance...")
        candidates.sort(key=lambda x: x[0])
        logs.append("Sort complete.")
        
        # 3. Greedy Selection with 30m Spacing
        selected_tuples: List[Tuple[float, POI]] = []
        min_spacing_km = 0.03 # 30 meters
        
        for dist_from_user, candidate_poi in candidates:
            if len(selected_tuples) >= max_results:
                logs.append(f"Max results ({max_results}) reached.")
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
                    logs.append(f"Skipping {candidate_poi.name}: Too close ({inter_poi_dist*1000:.1f}m) to {selected_poi.name}")
                    break
            
            if is_far_enough:
                logs.append(f"Spacing check passed for {candidate_poi.name} (checked against {len(selected_tuples)} selected)")
                try:
                    selected_tuples.append((dist_from_user, candidate_poi))
                    logs.append(f"Selected {candidate_poi.name} (Dist: {dist_from_user*1000:.1f}m)")
                except Exception as e:
                    logs.append(f"Error selecting {candidate_poi.name}: {e}")

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
        return results, logs
