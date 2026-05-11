import json
from dataclasses import dataclass
from typing import Optional

import structlog

from app.schemas.search import SearchRequest, SearchResponse, GaugeResult, SearchTarget
from app.services.bucket_engine import BucketEngine
from app.services.construction_score import construction_score
from app.services.report_renderer import ReportRenderer

logger = structlog.get_logger(__name__)


@dataclass
class SearchDependencies:
    redis: Optional[object]
    precompute_repo: object
    demand_service: object
    poi_service: Optional[object] = None


class SearchService:
    CACHE_TTL_SECONDS = 120
    LOCK_TTL_SECONDS = 15
    SIMULATED_FIXED_CONSTRUCTION_SCORE = 42

    def __init__(self, deps: SearchDependencies):
        self._deps = deps

    @staticmethod
    def _coord_key(lat: float, lon: float) -> str:
        return f"{lat:.4f},{lon:.4f}"

    @classmethod
    def _cache_key(cls, target: str, tier: str, lat: float, lon: float) -> str:
        return f"search:v2:{target}:{tier}:{lat:.4f}:{lon:.4f}"

    @classmethod
    def _lock_key(cls, target: str, tier: str, lat: float, lon: float) -> str:
        return f"search:v2:lock:{target}:{tier}:{lat:.4f}:{lon:.4f}"

    async def _compute_construction_score(
        self,
        lat: float,
        lon: float,
        cell_id: str,
        candidates: list,
    ) -> tuple[int, str]:
        """Compute construction score using tier-weighted + ambient density.

        Falls back to the legacy candidate-count formula when poi_service is
        unavailable or when any part of the new computation fails.
        """
        fallback_score = min(100, len(candidates) * 10)
        fallback = (fallback_score, "legacy_fallback")

        if self._deps.poi_service is None:
            logger.info(
                "construction_score_legacy_fallback_used",
                reason="poi_service_unavailable",
                cell_id=cell_id,
            )
            return fallback

        try:
            bins = await self._deps.poi_service.get_construction_distance_bins(lat, lon)
            tier_counts = [
                int(bins.get("0_10", 0)),
                int(bins.get("10_20", 0)),
                int(bins.get("20_30", 0)),
                int(bins.get("30_40", 0)),
            ]

            cell_stats = await self._deps.precompute_repo.get_cell_stats(cell_id)
            grid_count = int(cell_stats.get("grid_poi_count", 0))

            percentiles = await self._deps.precompute_repo.get_grid_percentiles(
                redis=getattr(self._deps, "redis", None)
            )
            grid_p99 = float(percentiles.get("p99", 0.0))

            score = construction_score(tier_counts, grid_count, grid_p99)
            logger.info(
                "construction_score_computed",
                tier_counts=tier_counts,
                grid_count=grid_count,
                grid_p99=grid_p99,
                score=score,
                cell_id=cell_id,
            )
            return score, "new_algorithm"

        except Exception as exc:
            logger.warning(
                "construction_score_new_algorithm_failed",
                error=str(exc),
                lat=round(lat, 5),
                lon=round(lon, 5),
                cell_id=cell_id,
            )
            logger.info(
                "construction_score_legacy_fallback_used",
                reason="new_algorithm_error",
                cell_id=cell_id,
            )
            return fallback

    async def run(self, *, request: SearchRequest, tier: str, quota_remaining: int, checks_today: int) -> SearchResponse:
        target = request.target.value
        cache_key = self._cache_key(target, tier, request.lat, request.lon)
        lock_key = self._lock_key(target, tier, request.lat, request.lon)
        lock_acquired = False
        lock_acquire_error = False

        if self._deps.redis:
            try:
                cached = await self._deps.redis.get(cache_key)
                if cached:
                    payload = SearchResponse.model_validate(json.loads(cached))
                    if payload.construction:
                        payload.construction.cached = True
                    if payload.demand:
                        payload.demand.cached = True
                    payload.message_code = "CACHE_HIT"
                    payload.message = "Served from cache"
                    payload.quota_remaining = quota_remaining
                    payload.checks_today = checks_today
                    payload.tier = tier
                    return payload
            except Exception as exc:
                logger.warning(
                    "search_service_cache_read_failed",
                    error=str(exc),
                    cache_key=cache_key,
                    description="Failed to read search cache; continuing with live compute.",
                )

            try:
                lock_acquired = bool(await self._deps.redis.set(lock_key, "1", nx=True, ex=self.LOCK_TTL_SECONDS))
            except Exception as exc:
                logger.warning(
                    "search_service_lock_acquire_failed",
                    error=str(exc),
                    lock_key=lock_key,
                    description="Failed to acquire in-flight lock; proceeding without lock safety.",
                )
                lock_acquire_error = True
                lock_acquired = False

            if self._deps.redis and not lock_acquired and not lock_acquire_error:
                return SearchResponse(
                    construction=None,
                    demand=None,
                    message_code="IN_FLIGHT",
                    message="Search is already processing. Retry shortly.",
                    quota_remaining=quota_remaining,
                    checks_today=checks_today,
                    tier=tier,
                )

        try:
            coord_key = self._coord_key(request.lat, request.lon)
            cell_id = BucketEngine.get_cell_id(request.lat, request.lon)
            candidates = await self._deps.precompute_repo.get_candidates(cell_id)

            construction = None
            demand = None
            if request.target in (SearchTarget.CONSTRUCTION, SearchTarget.BOTH):
                construction_score_value, score_source = await self._compute_construction_score(
                    lat=request.lat,
                    lon=request.lon,
                    cell_id=cell_id,
                    candidates=candidates,
                )
                construction = GaugeResult(
                    score=construction_score_value,
                    score_source=score_source,
                    coord_key=coord_key,
                    message_code="CONSTRUCTION_READY",
                    message="Construction analysis complete",
                )

            if request.target in (SearchTarget.DEMAND, SearchTarget.BOTH):
                rolling = await self._deps.demand_service.get_demand_rolling(cell_id)
                demand_score = min(100, rolling)
                demand = GaugeResult(
                    score=demand_score,
                    coord_key=coord_key,
                    message_code="DEMAND_READY",
                    message="Demand analysis complete",
                )

            response = SearchResponse(
                construction=construction,
                demand=demand,
                message_code="SEARCH_COMPLETE",
                message=ReportRenderer.render(candidates, request.lat, request.lon, limit=1)[0].text if candidates else "No nearby signals",
                quota_remaining=quota_remaining,
                checks_today=checks_today,
                tier=tier,
            )

            if self._deps.redis:
                try:
                    await self._deps.redis.set(cache_key, response.model_dump_json(), ex=self.CACHE_TTL_SECONDS)
                except Exception as exc:
                    logger.warning(
                        "search_service_cache_write_failed",
                        error=str(exc),
                        cache_key=cache_key,
                        description="Failed to write search cache; response still returned.",
                    )
            return response
        except Exception as exc:
            logger.error(
                "search_service_compute_failed",
                error=str(exc),
                target=target,
                description="Search compute pipeline failed unexpectedly.",
            )
            raise
        finally:
            if self._deps.redis and lock_acquired:
                try:
                    await self._deps.redis.delete(lock_key)
                except Exception as exc:
                    logger.warning(
                        "search_service_lock_release_failed",
                        error=str(exc),
                        lock_key=lock_key,
                        description="Failed to release in-flight search lock.",
                    )
