
import asyncio
import sys
import os
import httpx
from typing import Any

# Add the current directory to sys.path
sys.path.append(os.getcwd())

from app.core.config import settings
from app.utils.haversine import haversine

# Ground Truth: Hiyori Garden Tower
HIYORI_LAT = 16.0613474
HIYORI_LON = 108.2357775

# Test Coordinates (from User)
TEST_LAT = 16.061531
TEST_LON = 108.235666

async def test_coordinates():
    print(f"--- Distance Validation ---")
    print(f"Hiyori (Ground Truth): {HIYORI_LAT}, {HIYORI_LON}")
    print(f"Test Coordinates:      {TEST_LAT}, {TEST_LON}")
    
    # Calculate Haversine
    dist_km = haversine(TEST_LAT, TEST_LON, HIYORI_LAT, HIYORI_LON)
    dist_m = dist_km * 1000
    print(f"Calculated Distance:   {dist_m:.2f} meters")
    
    print(f"\n--- Mapbox API Coordinate Support ---")
    
    if not settings.MAPBOX_TOKEN:
        print("Skipping Mapbox check: MAPBOX_TOKEN is not set.")
        return

    # Check if Mapbox accepts "lon,lat" as a query (Reverse Geocoding)
    # Mapbox Geocoding API format: {longitude},{latitude}
    query = f"{TEST_LON},{TEST_LAT}"
    url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{query}.json"
    params = {
        "access_token": settings.MAPBOX_TOKEN,
        # "types": "address" # Optional, to restrict result types
    }
    
    async with httpx.AsyncClient() as client:
        try:
            print(f"Querying Mapbox with: {query}")
            resp = await client.get(url, params=params)
            data: dict[str, Any] = resp.json()
            
            features: list[dict[str, Any]] = data.get("features", [])
            
            if len(features) > 0:
                print(f"Mapbox returned {len(features)} features.")
                for i, f in enumerate(features[:3]):
                    place_name = f.get('place_name', 'Unknown')
                    place_type = f.get('place_type', ['unknown'])
                    center = f.get('center', [0, 0])
                    
                    print(f"[{i}] {place_name} ({place_type})")
                    print(f"    Center: {center}")
                    # Distance from returned center to Hiyori
                    mlon: float = float(center[0])
                    mlat: float = float(center[1])
                    mdist = haversine(mlat, mlon, HIYORI_LAT, HIYORI_LON) * 1000
                    print(f"    Dist to Hiyori: {mdist:.2f} meters")
            else:
                print("Mapbox returned NO results for these coordinates.")
                
        except Exception as e:
            print(f"Error querying Mapbox: {e}")

if __name__ == "__main__":
    asyncio.run(test_coordinates())
