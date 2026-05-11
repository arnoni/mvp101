
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
from app.core.db import build_asyncpg_url_and_connect_args
from app.services.bucket_engine import BucketEngine
import structlog

# Configure structlog for the job
structlog.configure(
    processors=[
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()


async def _precompute_cell_stats_and_percentiles(engine, poi_rows):
    """Precompute cell-level POI counts and the global p99 in one transaction.

    Distance bins remain query-time computations from the user's exact GPS
    coordinate. This job only prepares the ambient density signal used by the
    construction scoring algorithm. ``poi_rows`` reuses the rows already fetched
    by ``run_precompute`` and is expected to contain ``(id, name, category, lat, lon)``.
    """
    logger.info("cell_poi_stats_precompute_starting")

    cell_counts: Dict[str, int] = {}
    for row in poi_rows:
        lat = row[3]
        lon = row[4]
        if lat is None or lon is None:
            continue
        cell_id = BucketEngine.get_cell_id(float(lat), float(lon))
        cell_counts[cell_id] = cell_counts.get(cell_id, 0) + 1

    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE cell_poi_stats"))

        if cell_counts:
            await conn.execute(
                text(
                    "INSERT INTO cell_poi_stats (cell_id, grid_poi_count, updated_at) "
                    "VALUES (:cell_id, :grid_poi_count, now())"
                ),
                [
                    {"cell_id": cell_id, "grid_poi_count": count}
                    for cell_id, count in cell_counts.items()
                ],
            )

        await conn.execute(
            text(
                "INSERT INTO cell_poi_percentiles "
                "    (percentile, value, sample_size, updated_at) "
                "SELECT "
                "    99.0 AS percentile, "
                "    COALESCE("
                "        percentile_cont(0.99) "
                "            WITHIN GROUP (ORDER BY grid_poi_count), "
                "        0.0"
                "    ) AS value, "
                "    COUNT(*) AS sample_size, "
                "    now() AS updated_at "
                "FROM cell_poi_stats "
                "ON CONFLICT (percentile) "
                "DO UPDATE SET "
                "    value       = EXCLUDED.value, "
                "    sample_size = EXCLUDED.sample_size, "
                "    updated_at  = now()"
            )
        )

    logger.info(
        "cell_poi_stats_precompute_complete",
        cells_updated=len(cell_counts),
    )

async def run_precompute():
    logger.info("Starting Daily Precompute Job")

    if not settings.DATABASE_URL:
        logger.error("DATABASE_URL not set")
        return

    # Create engine
    url, connect_args = build_asyncpg_url_and_connect_args(settings.DATABASE_URL)
    engine = create_async_engine(url, connect_args=connect_args)

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

    # --- Cell-level stats + global percentiles (daily, atomic) ---
    try:
        await _precompute_cell_stats_and_percentiles(engine, raw_pois)
    except Exception as e:
        logger.error("cell_poi_stats_precompute_failed", error=str(e))

    await engine.dispose()
    logger.info("Daily precompute — all steps complete")

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_precompute())
