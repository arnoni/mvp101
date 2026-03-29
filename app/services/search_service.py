import json
from dataclasses import dataclass
from typing import Optional

from app.schemas.search import SearchRequest, SearchResponse, GaugeResult, SearchTarget
from app.services.bucket_engine import BucketEngine
from app.services.report_renderer import ReportRenderer


@dataclass
class SearchDependencies:
    redis: Optional[object]
    precompute_repo: object
    demand_service: object


class SearchService:
    CACHE_TTL_SECONDS = 120
    LOCK_TTL_SECONDS = 15

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

    async def run(self, *, request: SearchRequest, tier: str, quota_remaining: int, checks_today: int) -> SearchResponse:
        target = request.target.value
        cache_key = self._cache_key(target, tier, request.lat, request.lon)
        lock_key = self._lock_key(target, tier, request.lat, request.lon)

        if self._deps.redis:
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

            acquired = await self._deps.redis.set(lock_key, "1", nx=True, ex=self.LOCK_TTL_SECONDS)
            if not acquired:
                return SearchResponse(
                    construction=None,
                    demand=None,
                    message_code="IN_FLIGHT",
                    message="Search is already processing. Retry shortly.",
                    quota_remaining=quota_remaining,
                    checks_today=checks_today,
                    tier=tier,
                )

        coord_key = self._coord_key(request.lat, request.lon)
        cell_id = BucketEngine.get_cell_id(request.lat, request.lon)
        candidates = await self._deps.precompute_repo.get_candidates(cell_id)

        construction = None
        demand = None
        if request.target in (SearchTarget.CONSTRUCTION, SearchTarget.BOTH):
            construction_score = min(100, len(candidates) * 10)
            construction = GaugeResult(
                score=construction_score,
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
            await self._deps.redis.set(cache_key, response.model_dump_json(), ex=self.CACHE_TTL_SECONDS)
            await self._deps.redis.delete(lock_key)
        return response
