import json
from typing import List

import sentry_sdk
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.analytics import capture
from app.models.dto import PrecomputeCandidate

logger = structlog.get_logger(__name__)


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
                    payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    raw_list = payload.get("pois", []) if isinstance(payload, dict) else []
                    candidates = []
                    for item in raw_list:
                        try:
                            candidates.append(PrecomputeCandidate(**item))
                        except Exception as exc:
                            logger.warning(
                                "precompute_candidate_parse_failed",
                                event_code="E_PRECOMPUTE_CANDIDATE_PARSE_FAILED",
                                cell_id=cell_id,
                                candidate=item,
                                error_type=type(exc).__name__,
                                error_detail=str(exc),
                            )
                            sentry_sdk.capture_exception(exc)
                            continue
                    return candidates
        except Exception as exc:
            logger.error(
                "precompute_fetch_failed",
                event_code="E_PRECOMPUTE_FETCH_FAILED",
                cell_id=cell_id,
                error_type=type(exc).__name__,
                error_detail=str(exc),
            )
            sentry_sdk.capture_exception(exc)
            return []

        return []

    async def get_cell_stats(self, cell_id: str) -> dict[str, int]:
        """Returns {"grid_poi_count": N} from cell_poi_stats."""
        if not self.db_engine:
            logger.warning("get_cell_stats_no_engine", cell_id=cell_id)
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
                    result = {"grid_poi_count": int(row["grid_poi_count"])}
                    logger.info(
                        "cell_stats_fetched",
                        cell_id=cell_id,
                        grid_poi_count=result["grid_poi_count"],
                    )
                    capture(
                        user_id=cell_id,
                        event="cell_stats_fetched",
                        properties=result,
                    )
                    return result
        except Exception as exc:
            logger.error(
                "cell_stats_fetch_failed",
                cell_id=cell_id,
                error_type=type(exc).__name__,
                error_detail=str(exc),
            )
            sentry_sdk.capture_exception(exc)
            capture(
                user_id=cell_id or "unknown",
                event="cell_stats_failed",
                properties={"cell_id": cell_id, "error_type": type(exc).__name__},
            )

        return {"grid_poi_count": 0}

    async def get_grid_percentiles(self, redis: object = None) -> dict[str, float | int]:
        """Returns {"p99": <float>, "sample_size": <int>} from cached or DB stats."""
        if redis is not None:
            try:
                cached = await redis.get("grid_p99_cache")
                if cached is not None:
                    val = json.loads(cached)
                    logger.info("grid_percentiles_cache_hit", source="redis", p99=val)
                    return {"p99": float(val), "sample_size": -1}
            except Exception as exc:
                logger.error(
                    "grid_percentiles_redis_read_failed",
                    cache_key="grid_p99_cache",
                    error_type=type(exc).__name__,
                    error_detail=str(exc),
                )
                sentry_sdk.capture_exception(exc)

        if not self.db_engine:
            logger.warning("grid_percentiles_no_engine", source="fallback_zero")
            return {"p99": 0.0, "sample_size": 0}

        stmt = text(
            """
            SELECT
                COALESCE(
                    percentile_cont(0.99) WITHIN GROUP (ORDER BY grid_poi_count),
                    0.0
                ) AS p99,
                COUNT(*) AS sample_size
            FROM cell_poi_stats
            """
        )

        try:
            async with self.db_engine.connect() as conn:
                row = (await conn.execute(stmt)).mappings().first()
                p99 = float(row["p99"]) if row and row["p99"] is not None else 0.0
                sample_size = int(row["sample_size"]) if row else 0
        except Exception as exc:
            logger.error(
                "grid_percentiles_db_query_failed",
                source="db",
                error_type=type(exc).__name__,
                error_detail=str(exc),
            )
            sentry_sdk.capture_exception(exc)
            return {"p99": 0.0, "sample_size": 0}

        if redis is not None:
            try:
                await redis.set("grid_p99_cache", json.dumps(p99), ex=3600)
            except Exception as exc:
                logger.error(
                    "grid_percentiles_redis_write_failed",
                    cache_key="grid_p99_cache",
                    p99=p99,
                    error_type=type(exc).__name__,
                    error_detail=str(exc),
                )
                sentry_sdk.capture_exception(exc)

        logger.info(
            "grid_percentiles_fetched",
            source="db",
            p99=p99,
            sample_size=sample_size,
        )
        return {"p99": p99, "sample_size": sample_size}
