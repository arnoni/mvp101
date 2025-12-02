# Implements TSD FR-007: Optional KMZ Download
# Implements TSD Section 4.1: Service Layer (kmz_service)
# Implements TSD Section 4.2: KMZ Generation

import io
import zipfile
import logging
from typing import List
from fastkml import kml
from fastkml.styles import Style, IconStyle, LabelStyle
# fastkml newer versions expose Placemark via kml module
from fastkml.kml import Placemark
from fastkml.geometry import Point
# AltitudeMode enum removed; not needed for basic KMZ generation
from fastapi import HTTPException, status
from app.models.dto import PublicPOIResult, ErrorResponse

logger = logging.getLogger(__name__)

async def generate_kmz(results: List[PublicPOIResult]) -> bytes:
    """
    Generates a KMZ file containing the top 5 POI results.
    Implements TSD FR-007.
    """
    if not results:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="NO_RESULTS",
                detail="Cannot generate KMZ without results."
            ).model_dump()
        )

    # 1. Create KML document
    k = kml.KML()
    d = kml.Document(
        name="Geo-Proximity Lead Magnet Results",
        description="Top 5 nearest real-estate points of interest."
    )
    k.append(d)

    # 2. Add Placemarks for each result
    for i, poi in enumerate(results):
        # Create a Placemark
        pm = Placemark(
            name=poi.name,
            description=f"Distance: {poi.distance_km} km. <a href='{poi.google_maps_link}'>Navigate Here</a>",
            styleUrl="#icon-style" # Use a default style if defined
        )
        
        # Create the Point geometry
        pm.geometry = Point(
            coordinates=[poi.lon, poi.lat, 0]  # KML uses (lon, lat, alt)
        )
        
        d.append(pm)

    # 3. Serialize KML to string
    kml_string = k.to_string(prettyprint=True)

    # 4. Create KMZ (ZIP archive) in memory
    # Implements TSD Section 4.2: `zipfile`
    kmz_buffer = io.BytesIO()
    with zipfile.ZipFile(kmz_buffer, 'w', zipfile.ZIP_DEFLATED) as kmz_file:
        # The main KML file must be named doc.kml
        kmz_file.writestr('doc.kml', kml_string.encode('utf-8'))
        
        # In a real scenario, you might add images here if needed, but for this MVP, we skip images
        # as they are served via CDN/static files and not strictly required for the KMZ to function.

    kmz_buffer.seek(0)
    return kmz_buffer.read()
