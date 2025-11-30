# Implements TSD Section 4.4: Haversine formula (km)
# Implements TSD Section 7.2: Type hints mandatory

from math import radians, sin, cos, sqrt, asin
from typing import Tuple

# Earth's radius in kilometers
R = 6371.0

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two points 
    on the Earth (specified in decimal degrees) using the Haversine formula.

    Args:
        lat1: Latitude of point 1.
        lon1: Longitude of point 1.
        lat2: Latitude of point 2.
        lon2: Longitude of point 2.

    Returns:
        Distance between the two points in kilometers.
    """
    # Convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])

    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1

    a = sin(dlat / 2)**2 + cos(lat1) * cos(lat2) * sin(dlon / 2)**2
    c = 2 * asin(sqrt(a))
    
    distance = R * c
    
    return distance
