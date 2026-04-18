import math
from typing import List


class BucketEngine:
    """
    Grid-based bucketing using mathematical gridding with ~50m target cells.

    Cell ID format:
        "<cell_lat>,<cell_lon>"
    where (cell_lat, cell_lon) is the lower-left corner of the cell.
    """

    CELL_SIZE_METERS = 50.0
    EARTH_METERS_PER_DEGREE = 111_320.0
    CELL_ID_DECIMALS = 8

    @staticmethod
    def _meters_per_degree_lon(lat_deg: float) -> float:
        cos_lat = math.cos(math.radians(lat_deg))
        # Guard near poles where longitude degree width approaches zero.
        if abs(cos_lat) < 0.001:
            cos_lat = 0.001
        return BucketEngine.EARTH_METERS_PER_DEGREE * cos_lat

    @staticmethod
    def _deltas(lat_deg: float) -> tuple[float, float]:
        """
        Returns (delta_lat_deg, delta_lon_deg) for the given latitude.
        """
        delta_lat = BucketEngine.CELL_SIZE_METERS / BucketEngine.EARTH_METERS_PER_DEGREE
        meters_per_degree_lon = BucketEngine._meters_per_degree_lon(lat_deg)
        if math.isinf(meters_per_degree_lon):
            delta_lon = 360.0
        else:
            delta_lon = BucketEngine.CELL_SIZE_METERS / meters_per_degree_lon
        return delta_lat, delta_lon

    @staticmethod
    def get_gps_cell(lat: float, lon: float, cell_size_m: float | None = None) -> tuple[float, float]:
        """
        Returns (cell_lat, cell_lon) as the lower-left corner of the cell.
        """
        cell_size = cell_size_m if cell_size_m is not None else BucketEngine.CELL_SIZE_METERS

        delta_lat = cell_size / BucketEngine.EARTH_METERS_PER_DEGREE
        cell_lat = math.floor(lat / delta_lat) * delta_lat

        # Use snapped latitude for stable longitudinal gridding within a band.
        meters_per_degree_lon = BucketEngine._meters_per_degree_lon(cell_lat)
        delta_lon = cell_size / meters_per_degree_lon

        cell_lon = math.floor(lon / delta_lon) * delta_lon

        return round(cell_lat, BucketEngine.CELL_ID_DECIMALS), round(cell_lon, BucketEngine.CELL_ID_DECIMALS)

    @staticmethod
    def get_cell_id(lat: float, lon: float) -> str:
        """
        Returns a unique string ID for the ~50m cell containing (lat, lon).
        The ID is encoded as "<lower_left_lat>,<lower_left_lon>".
        """
        cell_lat, cell_lon = BucketEngine.get_gps_cell(lat, lon)
        return f"{cell_lat:.{BucketEngine.CELL_ID_DECIMALS}f},{cell_lon:.{BucketEngine.CELL_ID_DECIMALS}f}"

    @staticmethod
    def get_center(cell_id: str) -> tuple[float, float]:
        """
        Returns the approximate center (lat, lon) of a cell from its ID.
        """
        try:
            parts = cell_id.split(",")
            cell_lat = float(parts[0])
            cell_lon = float(parts[1])

            delta_lat, delta_lon = BucketEngine._deltas(cell_lat)

            lat = cell_lat + (delta_lat / 2)
            lon = cell_lon + (delta_lon / 2)
            return round(lat, 9), round(lon, 9)
        except (ValueError, IndexError):
            return 0.0, 0.0

    @staticmethod
    def get_cell_dimensions_m(lat: float) -> tuple[float, float, float]:
        """
        Returns approximate cell dimensions and area at the given latitude.

        Returns:
            (north_south_m, east_west_m, area_m2)
        """
        north_south_m = BucketEngine.CELL_SIZE_METERS
        east_west_m = BucketEngine.CELL_SIZE_METERS
        area_m2 = north_south_m * east_west_m
        return north_south_m, east_west_m, area_m2

    @staticmethod
    def get_adjacent_cell_ids(lat: float, lon: float, radius: int = 1) -> List[str]:
        """
        Returns cell IDs in a square neighborhood around the origin cell.

        For radius=1, returns a 3x3 grid (9 cells) centered on the origin cell.
        """
        origin_lat, origin_lon = BucketEngine.get_gps_cell(lat, lon)
        delta_lat, delta_lon = BucketEngine._deltas(origin_lat)

        cell_ids: List[str] = []
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                adj_lat = origin_lat + (dy * delta_lat)
                adj_lon = origin_lon + (dx * delta_lon)
                cell_ids.append(
                    f"{adj_lat:.{BucketEngine.CELL_ID_DECIMALS}f},{adj_lon:.{BucketEngine.CELL_ID_DECIMALS}f}"
                )

        return cell_ids
