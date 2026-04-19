
import asyncio
import json
import os
import sys
from typing import Dict, List, Tuple

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.core.config import settings
from app.services.bucket_engine import BucketEngine
import structlog

# Configure structlog for the job
structlog.configure(
    processors=[
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

async def run_precompute():
    logger.info("Starting Daily Precompute Job")

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

    # 3. Build payload for each exact 50m cell
    precomputed_data = []  # List of dicts to insert

    for cell_id, pois in cells.items():
        if not pois:
            continue
        candidate_list = []
        for p in pois:
            pid, name, category, lat, lon = p
            cand = {
                "id": str(pid),
                "lat": lat,
                "lon": lon,
                "category": category or "Uncategorized",
                "name": name,
            }
            candidate_list.append(cand)

        payload = {
            "metadata": {
                "poi_count": len(candidate_list),
            },
            "pois": candidate_list,
        }

        precomputed_data.append({
            "cell_id": cell_id,
            "data": json.dumps(payload),
        })

    # 4. Replace all precomputed rows to avoid ghost data from removed POIs
    TRUNCATE_SQL = text("TRUNCATE TABLE cell_poi_precompute")
    INSERT_SQL = text("""
        INSERT INTO cell_poi_precompute (cell_id, data, updated_at)
        VALUES (:cell_id, :data, now())
    """)
    
    async with engine.begin() as conn:
        try:
            await conn.execute(TRUNCATE_SQL)
            if precomputed_data:
                await conn.execute(INSERT_SQL, precomputed_data)
                logger.info("insert_complete", cells_updated=len(precomputed_data))
            else:
                logger.info("no_data_to_insert")
        except Exception as e:
            logger.error("insert_failed", error=str(e))

    await engine.dispose()
    logger.info("Job Complete")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_precompute())
