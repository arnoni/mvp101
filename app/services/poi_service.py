# Implements TSD Section 4.1: Repository Pattern for MasterList (in-memory cache on startup)
# Implements TSD FR-005: Find Nearest 5 POIs
# Implements TSD Section 8: Never expose internal_notes or full MasterList

import json
import os
import logging
from typing import List, Tuple

from app.models.dto import POI, PublicPOIResult
from app.utils.haversine import haversine
from app.core.config import settings

logger = logging.getLogger(__name__)

class POIService:
    """
    Service layer for handling Point of Interest (POI) data.
    Loads the MasterList into memory on initialization.
    """
    def __init__(self):
        self.master_list: List[POI] = []
        self._load_master_list()

    def _load_master_list(self):
        """Loads the MasterList.json file into memory."""
        # Implements TSD Section 7.1: /static/masterlist.json
        file_path = os.path.join(os.path.dirname(__file__), "..", "..", "static", "masterlist.json")
        
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
            
            # Validate and parse the data using the Pydantic model
            master_list_model = POI.model_validate_json(json.dumps(data))
            self.master_list = master_list_model.points
            logger.info(f"Successfully loaded {len(self.master_list)} POIs from MasterList.")
            
        except FileNotFoundError:
            logger.error(f"MasterList file not found at: {file_path}")
            # TSD Section 8: Default to safe/secure option (empty list)
            self.master_list = []
        except Exception as e:
            logger.error(f"Error loading or validating MasterList: {e}")
            self.master_list = []

    def find_nearest_pois(self, user_lat: float, user_lon: float) -> List[PublicPOIResult]:
        """
        Calculates the distance to all POIs and returns the nearest 5.
        Implements TSD FR-005: Find Nearest 5 POIs.
        """
        if not self.master_list:
            return []

        # 6. Haversine against all points (in-memory list)
        distances: List[Tuple[float, POI]] = []
        for poi in self.master_list:
            dist_km = haversine(user_lat, user_lon, poi.lat, poi.lon)
            distances.append((dist_km, poi))

        # Sort by distance and take the top 5
        # Implements TSD Section 4.4: nearest_5 = sorted(distances)[:5]
        nearest_5 = sorted(distances, key=lambda x: x[0])[:5]

        # 7. Build DTOs
        results: List[PublicPOIResult] = []
        for dist_km, poi in nearest_5:
            # Implements TSD FR-006: "Go There” Google Maps universal link
            google_maps_link = (
                f"https://www.google.com/maps/dir/?api=1&destination={poi.lat},{poi.lon}"
            )
            
            # Implements TSD Section 4.2: image_url
            # Assuming images are served from /static/images/
            image_url = f"/static/images/{poi.images[0]}" if poi.images else ""

            results.append(
                PublicPOIResult(
                    name=poi.name,
                    distance_km=round(dist_km, 2), # Round to 2 decimal places for display
                    google_maps_link=google_maps_link,
                    image_url=image_url,
                    lat=poi.lat,
                    lon=poi.lon
                )
            )
        
        return results
