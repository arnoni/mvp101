import math

class BucketEngine:
    """
    Implements a simple grid-based bucketing system.
    Resolution: 0.005 degrees (approx 550 meters at equator).
    """
    
    RESOLUTION = 0.005
    
    @staticmethod
    def get_cell_id(lat: float, lon: float) -> str:
        """
        Returns a unique string ID for the cell containing (lat, lon).
        """
        lat_idx = math.floor(lat / BucketEngine.RESOLUTION)
        lon_idx = math.floor(lon / BucketEngine.RESOLUTION)
        return f"{lat_idx}_{lon_idx}"

    @staticmethod
    def get_center(cell_id: str) -> tuple[float, float]:
        """
        Returns the approx center (lat, lon) of a cell.
        """
        try:
            parts = cell_id.split("_")
            lat_idx = int(parts[0])
            lon_idx = int(parts[1])
            
            lat = (lat_idx * BucketEngine.RESOLUTION) + (BucketEngine.RESOLUTION / 2)
            lon = (lon_idx * BucketEngine.RESOLUTION) + (BucketEngine.RESOLUTION / 2)
            return round(lat, 6), round(lon, 6)
        except (ValueError, IndexError):
            return 0.0, 0.0
