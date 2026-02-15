import math
from typing import List
from app.models.dto import PrecomputeCandidate, ReportLine

class ReportRenderer:
    """
    Renders opaque reports from raw candidates.
    Computing distance internally but exposing only rounded/opaque text.
    """
    
    @staticmethod
    def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
        """
        Calculate distance in meters.
        """
        R = 6371000  # Radius of Earth in meters
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        
        a = math.sin(delta_phi / 2.0) ** 2 + \
            math.cos(phi1) * math.cos(phi2) * \
            math.sin(delta_lambda / 2.0) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        
        return int(R * c)

    @staticmethod
    def render(
        candidates: List[PrecomputeCandidate], 
        user_lat: float, 
        user_lon: float, 
        limit: int = 5
    ) -> List[ReportLine]:
        """
        Sorts candidates by distance and returns opaque ReportLines.
        """
        # 1. Calc distances
        scored = []
        for c in candidates:
            dist = ReportRenderer.haversine_distance(user_lat, user_lon, c.lat, c.lon)
            scored.append((dist, c))
            
        # 2. Sort
        scored.sort(key=lambda x: x[0])
        
        # 3. Take top K
        top_k = scored[:limit]
        
        # 4. Render
        lines = []
        for dist, c in top_k:
            # Round distance to nearest 50m for privacy
            rounded_dist = round(dist / 50) * 50
            if rounded_dist == 0:
                rounded_dist = 50 # "Nearby"
                
            text = f"{c.category} (~{rounded_dist}m)"
            lines.append(ReportLine(text=text, category=c.category))
            
        return lines
