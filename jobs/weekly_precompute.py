
import asyncio
import os
import sys
import json
import math
from typing import List, Dict, Tuple

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.core.config import settings
from app.services.bucket_engine import BucketEngine
from app.utils.haversine import haversine
import structlog

# Configure structlog for the job
structlog.configure(
    processors=[
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

async def run_precompute():
    logger.info("Starting Weekly Precompute Job")
    
    if not settings.DATABASE_URL:
        logger.error("DATABASE_URL not set")
        return

    # Create engine (handling sslmode quirk)
    url = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    if "?sslmode=" in url or "&sslmode=" in url:
        url = url.replace("?sslmode=require", "").replace("&sslmode=require", "")
        url = url.replace("?channel_binding=require", "").replace("&channel_binding=require", "")
        
    engine = create_async_engine(url)

    # 1. Fetch ALL POIs
    # In a real heavy production system, we'd paginate or stream. 
    # For MVP (Da Nang), fetching all is likely fine (thousands of rows).
    FETCH_ALL_SQL = text("SELECT id, name, category, ST_Y(geom::geometry) as lat, ST_X(geom::geometry) as lon FROM pois")
    
    raw_pois = []
    try:
        async with engine.connect() as conn:
            result = await conn.execute(FETCH_ALL_SQL)
            raw_pois = result.fetchall()
    except Exception as e:
        logger.error("failed_fetch_pois", error=str(e))
        return

    logger.info("fetched_pois", count=len(raw_pois))
    
    # 2. Bucket POIs into Cells
    cells: Dict[str, List[Tuple]] = {}
    
    for row in raw_pois:
        pid, name, category, lat, lon = row
        if not lat or not lon:
            continue
            
        cell_id = BucketEngine.get_cell_id(lat, lon)
        if cell_id not in cells:
            cells[cell_id] = []
        cells[cell_id].append((pid, name, category, lat, lon))
        
    logger.info("bucketed_cells", count=len(cells))
    
    # 3. Compute Candidates per Cell
    # Strategy: For each cell, we want the "best" POIs *for a user in that cell*.
    # Since the cell is small (500m), we can just take the cell center, calculate distance from center to POIs,
    # and sort by proximity to center + randomness or score?
    # Simple strategy: Sort by distance to cell center.
    
    # We also need to consider spacing (don't show two coffee shops right next to each other).
    # This logic matches ReportRenderer intentions but done at precompute time for candidates.
    
    precomputed_data = [] # List of dicts to insert
    
    for cell_id, pois in cells.items():
        center_lat, center_lon = BucketEngine.get_center(cell_id)
        
        # Helper to calc distance to center
        def dist_to_center(p):
            return haversine(center_lat, center_lon, p[3], p[4])
            
        # Sort by distance to center
        pois.sort(key=dist_to_center)
        
        # Take top N candidates (e.g. 50) to store in JSON
        # We store more than we show so we have backup/rotation
        top_candidates = pois[:50]
        
        candidate_list = []
        for p in top_candidates:
            pid, name, category, lat, lon = p
            # Create Candidate Dict
            # Hashing name for privacy? No, the PrecomputeRepo does obfuscation/hashing if needed?
            # actually ReportRenderer does. We store raw here?
            # TSD says "PrecomputeRepository returns Candidate objects (not ORM)".
            # Let's store raw-ish data but minimal.
            cand = {
                "lat": lat,
                "lon": lon,
                "category": category or "Uncategorized",
                "name_hash": str(hash(name)), # Simple hash for id stability/dedup
                # We might want to store more metadata like rating if we had it
            }
            candidate_list.append(cand)
            
        precomputed_data.append({
            "cell_id": cell_id,
            "data": json.dumps(candidate_list)
        })

    # 4. Bulk Upsert into cell_poi_precompute
    # We use INSERT ... ON CONFLICT DO UPDATE
    UPSERT_SQL = text("""
        INSERT INTO cell_poi_precompute (cell_id, data, updated_at)
        VALUES (:cell_id, :data, now())
        ON CONFLICT (cell_id) DO UPDATE SET
        data = EXCLUDED.data,
        updated_at = now()
    """)
    
    async with engine.begin() as conn:
        # Batch insert? SQLAlchemy execute with list of dicts does executemany
        try:
            if precomputed_data:
                await conn.execute(UPSERT_SQL, precomputed_data)
                logger.info("upsert_complete", cells_updated=len(precomputed_data))
            else:
                logger.info("no_data_to_upsert")
        except Exception as e:
            logger.error("upsert_failed", error=str(e))

    await engine.dispose()
    logger.info("Job Complete")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_precompute())
