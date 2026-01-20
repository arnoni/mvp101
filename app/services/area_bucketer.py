import math

class AreaBucketer:
    """
    Service for spatial quantization to support area-based throttling.
    Target precision: ~150m-600m.
    """

    @staticmethod
    def get_area_code(lat: float, lon: float, precision: int = 3) -> str:
        """
        Generates a simplified 'area code' by rounding coordinates.
        
        Precision guide (approximate at equator):
        - 2 decimal places: ~1.11 km
        - 3 decimal places: ~111 m
        - 4 decimal places: ~11 m
        
        We target ~150m-600m, so 3 decimal places is a good starting point (approx 100m bucket).
        We can use a slightly larger bucket by using a custom rounding logic (e.g. round to nearest 0.005).
        
        Args:
            lat: Latitude
            lon: Longitude
            precision: Number of decimal places to round to.
            
        Returns:
            String representation of the area bucket (e.g., "16.061,108.235")
        """
        # Option: Round to nearest 0.002 degrees (~220m) for slightly larger buckets than 0.001
        # lat_bucket = round(lat / 0.002) * 0.002
        # lon_bucket = round(lon / 0.002) * 0.002
        # return f"{lat_bucket:.3f},{lon_bucket:.3f}"
        
        # Simple implementation using standard rounding for now as per plan suggestion "simple lat/lng rounding"
        return f"{round(lat, precision)},{round(lon, precision)}"
