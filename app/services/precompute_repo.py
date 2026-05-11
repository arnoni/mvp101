import json
import logging
from typing import List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.dto import PrecomputeCandidate

logger = logging.getLogger(__name__)

class PrecomputeRepository:
    """
    Fetches precomputed POI candidates for a given cell.
    """
    
    def __init__(self, db_engine: AsyncEngine):
        self.db_engine = db_engine
        
    async def get_candidates(self, cell_id: str) -> List[PrecomputeCandidate]:
        """
        Retrieves the list of candidates for a cell.
        Returns empty list if no data or error.
        """
        if not self.db_engine:
            return []
            
        stmt = text("SELECT data FROM cell_poi_precompute WHERE cell_id = :cell_id")
        
        try:
            async with self.db_engine.connect() as conn:
                result = await conn.execute(stmt, {"cell_id": cell_id})
                row = result.fetchone()
                
                if row and row[0]:
                    # row[0] is the JSONB data column
                    payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    raw_list = payload.get("pois", []) if isinstance(payload, dict) else []
                    # Parse into Pydantic models
                    candidates = []
                    for item in raw_list:
                        try:
                            candidates.append(PrecomputeCandidate(**item))
                        except Exception as exc:
                            logger.warning(
                                "E_PRECOMPUTE_CANDIDATE_PARSE_FAILED skipping malformed precompute candidate",
                                extra={"event_code": "E_PRECOMPUTE_CANDIDATE_PARSE_FAILED", "cell_id": cell_id, "candidate": item},
                                exc_info=True,
                            )
                            continue
                    return candidates
        except Exception:
            logger.error(
                "E_PRECOMPUTE_FETCH_FAILED failed loading precompute candidates; returning empty list",
                extra={"event_code": "E_PRECOMPUTE_FETCH_FAILED", "cell_id": cell_id},
                exc_info=True,
            )
            return []
            
        return []

    async def get_cell_stats(self, cell_id: str) -> dict[str, int]:
        """Returns {"grid_poi_count": N} from cell_poi_stats.

        grid_poi_count is the number of raw POIs whose GPS snaps to this
        specific cell (NOT including neighboring cells). The ambient component
        measures local density relative to the global distribution; the
        tier-weighted component handles the neighborhood via query-time
        distance rings.

        Returns {"grid_poi_count": 0} if engine is None or query fails.
        """
        if not self.db_engine:
            return {"grid_poi_count": 0}

        stmt = text(
            """
            SELECT grid_poi_count
            FROM cell_poi_stats
            WHERE cell_id = :cell_id
            """
        )

        try:
            async with self.db_engine.connect() as conn:
                row = (await conn.execute(stmt, {"cell_id": cell_id})).mappings().first()
                if row:
                    return {"grid_poi_count": int(row["grid_poi_count"])}
        except Exception:
            logger.exception("cell_stats_fetch_failed")

        return {"grid_poi_count": 0}

    async def get_grid_percentiles(self, redis: object = None) -> dict[str, float | int]:
        """Returns {"p99": <float>, "sample_size": <int>} from cell_poi_percentiles.

        Single global value (percentile = 99.0), updated daily by the
        precompute job.

        Redis caching:
          Key:   "grid_p99_cache"
          Value: JSON-serialized float
          TTL:   3600 seconds (1 hour)
        Falls back to direct DB query if redis is None or read fails.
        Writes back to Redis on successful DB read.

        Returns {"p99": 0.0, "sample_size": 0} on any failure.
        """
        if redis is not None:
            try:
                cached = await redis.get("grid_p99_cache")
                if cached is not None:
                    val = json.loads(cached)
                    return {"p99": float(val), "sample_size": -1}
            except Exception:
                logger.exception("grid_percentiles_redis_read_failed")

        if not self.db_engine:
            return {"p99": 0.0, "sample_size": 0}

        stmt = text(
            """
            SELECT value AS p99, sample_size
            FROM cell_poi_percentiles
            WHERE percentile = 99.0
            """
        )

        try:
            async with self.db_engine.connect() as conn:
                row = (await conn.execute(stmt)).mappings().first()
                p99 = float(row["p99"]) if row and row["p99"] is not None else 0.0
                sample_size = int(row["sample_size"]) if row else 0
        except Exception:
            logger.exception("grid_percentiles_db_query_failed")
            return {"p99": 0.0, "sample_size": 0}

        if redis is not None:
            try:
                await redis.set("grid_p99_cache", json.dumps(p99), ex=3600)
            except Exception:
                logger.exception("grid_percentiles_redis_write_failed")

        return {"p99": p99, "sample_size": sample_size}
