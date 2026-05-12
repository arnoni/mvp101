import json
import time
from dataclasses import dataclass
from typing import Optional

import sentry_sdk
import structlog

from app.core.analytics import capture
from app.schemas.search import GaugeResult, SearchRequest, SearchResponse, SearchTarget
from app.services.bucket_engine import BucketEngine
from app.services.construction_score import construction_score
from app.services.report_renderer import ReportRenderer

logger = structlog.get_logger(__name__)

_previous_scores: dict[str, dict] = {}


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
        self._last_tier_counts: list[int] | None = None

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
        target: str,
    ) -> tuple[int, str, float | None]:
        """Compute construction score using tier-weighted + ambient density."""
        start_ms = time.time()
        fallback = (min(100, len(candidates) * 10), "legacy_fallback", None)

        if self._deps.poi_service is None:
            logger.info(
                "construction_score_legacy_fallback",
                reason="poi_service_unavailable",
                cell_id=cell_id,
                lat=round(lat, 5),
                lon=round(lon, 5),
                candidate_count=len(candidates),
                fallback_score=fallback[0],
            )
            capture(
                user_id=cell_id or "unknown",
                event="construction_score_fallback_v2",
                properties={
                    "reason": "poi_service_none",
                    "cell_id": cell_id,
                    "candidate_count": len(candidates),
                    "score": fallback[0],
                },
            )
            return fallback

        try:
            bins, min_dist_m = await self._deps.poi_service.get_construction_distance_bins(lat, lon)
            tier_counts = [
                int(bins.get("0_10", 0)),
                int(bins.get("10_20", 0)),
                int(bins.get("20_30", 0)),
                int(bins.get("30_40", 0)),
            ]
            self._last_tier_counts = tier_counts
            min_dist_for_log = round(min_dist_m, 2) if min_dist_m != float("inf") else None
            logger.debug(
                "construction_tier_counts",
                tier_counts=tier_counts,
                min_dist_m=min_dist_for_log,
                cell_id=cell_id,
                lat=round(lat, 5),
                lon=round(lon, 5),
            )
            total_tier = sum(tier_counts)
            if total_tier > 20:
                logger.warning(
                    "construction_tier_counts_anomaly",
                    tier_counts=tier_counts,
                    total=total_tier,
                    cell_id=cell_id,
                    lat=round(lat, 5),
                    lon=round(lon, 5),
                    detail="tier_counts sum exceeds 20. Spatial query likely returning "
                    "duplicate/cartesian rows.",
                )

            cell_stats = await self._deps.precompute_repo.get_cell_stats(cell_id)
            grid_count = int(cell_stats.get("grid_poi_count", 0))

            percentiles = await self._deps.precompute_repo.get_grid_percentiles(
                redis=getattr(self._deps, "redis", None)
            )
            grid_p99 = float(percentiles.get("p99", 0.0))
            grid_sample_size = int(percentiles.get("sample_size", 0))

            score = construction_score(tier_counts, grid_count, grid_p99, grid_sample_size)
            duration_ms = round((time.time() - start_ms) * 1000, 1)

            score_source = "new_algorithm"
            logger.info(
                "search_result_scored",
                score=score,
                tier_counts=tier_counts,
                min_dist_m=min_dist_for_log,
                cell_id=cell_id,
                target=target,
                source=score_source,
            )
            logger.info(
                "construction_score_computed",
                tier_counts=tier_counts,
                grid_count=grid_count,
                grid_p99=grid_p99,
                grid_sample_size=grid_sample_size,
                min_dist_m=min_dist_for_log,
                score=score,
                cell_id=cell_id,
                lat=round(lat, 5),
                lon=round(lon, 5),
                duration_ms=duration_ms,
                source=score_source,
            )
            capture(
                user_id=cell_id or "unknown",
                event="construction_score_computed_v2",
                properties={
                    "score": score,
                    "source": score_source,
                    "tier_counts": tier_counts,
                    "grid_count": grid_count,
                    "grid_p99": grid_p99,
                    "grid_sample_size": grid_sample_size,
                    "min_dist_m": min_dist_for_log,
                    "cell_id": cell_id,
                    "duration_ms": duration_ms,
                },
            )
            return score, score_source, min_dist_for_log

        except Exception as exc:
            duration_ms = round((time.time() - start_ms) * 1000, 1)
            logger.error(
                "construction_score_new_algorithm_failed",
                error_type=type(exc).__name__,
                error_detail=str(exc),
                cell_id=cell_id,
                lat=round(lat, 5),
                lon=round(lon, 5),
                candidate_count=len(candidates),
                duration_ms=duration_ms,
            )
            sentry_sdk.capture_exception(exc)
            capture(
                user_id=cell_id or "unknown",
                event="construction_score_fallback_v2",
                properties={
                    "reason": "exception",
                    "error_type": type(exc).__name__,
                    "cell_id": cell_id,
                    "candidate_count": len(candidates),
                    "score": fallback[0],
                },
            )
            return fallback

    async def run(
        self,
        *,
        request: SearchRequest,
        tier: str,
        quota_remaining: int,
        checks_today: int,
        user_id: str | None = None,
        anon_id: str | None = None,
        session_id: str | None = None,
        attempt_id: str | None = None,
    ) -> SearchResponse:
        target = request.target.value
        cell_id = BucketEngine.get_cell_id(request.lat, request.lon)
        cache_key = self._cache_key(target, tier, request.lat, request.lon)
        lock_key = self._lock_key(target, tier, request.lat, request.lon)
        lock_acquired = False
        lock_acquire_error = False
        self._last_tier_counts = None

        logger.info(
            "search_service_request_started",
            target=target,
            tier=tier,
            cell_id=cell_id,
            lat=round(request.lat, 5),
            lon=round(request.lon, 5),
            quota_remaining=quota_remaining,
            checks_today=checks_today,
        )
        sentry_sdk.add_breadcrumb(
            message="search_service_run",
            category="search",
            level="info",
            data={
                "target": target,
                "tier": tier,
                "cell_id": cell_id,
                "lat": round(request.lat, 5),
                "lon": round(request.lon, 5),
            },
        )

        if self._deps.redis:
            try:
                cached = await self._deps.redis.get(cache_key)
                if cached:
                    payload = SearchResponse.model_validate(json.loads(cached))
                    if payload.construction:
                        payload.construction.cached = True
                    if payload.demand:
                        payload.demand.cached = True
                    if payload.construction and payload.construction.score is not None:
                        self._record_previous_score(
                            score=payload.construction.score,
                            cell_id=cell_id,
                            user_id=user_id,
                            anon_id=anon_id,
                            session_id=session_id,
                            attempt_id=attempt_id,
                            min_dist_m=None,
                        )
                    payload.message_code = "CACHE_HIT"
                    payload.message = "Served from cache"
                    payload.quota_remaining = quota_remaining
                    payload.checks_today = checks_today
                    payload.tier = tier
                    capture(
                        user_id=cell_id or "unknown",
                        event="search_cache_hit",
                        properties={
                            "target": target,
                            "tier": tier,
                            "cell_id": cell_id,
                        },
                    )
                    self._log_completion(
                        request=request,
                        tier=tier,
                        cell_id=cell_id,
                        construction=payload.construction,
                        demand=payload.demand,
                        quota_remaining=quota_remaining,
                        checks_today=checks_today,
                        payload=payload,
                    )
                    return payload
            except Exception as exc:
                logger.warning(
                    "search_service_cache_read_failed",
                    error_type=type(exc).__name__,
                    error_detail=str(exc),
                    cache_key=cache_key,
                    description="Failed to read search cache; continuing with live compute.",
                )
                sentry_sdk.capture_exception(exc)

            try:
                lock_acquired = bool(
                    await self._deps.redis.set(lock_key, "1", nx=True, ex=self.LOCK_TTL_SECONDS)
                )
            except Exception as exc:
                logger.warning(
                    "search_service_lock_acquire_failed",
                    error_type=type(exc).__name__,
                    error_detail=str(exc),
                    lock_key=lock_key,
                    description="Failed to acquire in-flight lock; proceeding without lock safety.",
                )
                sentry_sdk.capture_exception(exc)
                lock_acquire_error = True
                lock_acquired = False

            if self._deps.redis and not lock_acquired and not lock_acquire_error:
                response = SearchResponse(
                    construction=None,
                    demand=None,
                    message_code="IN_FLIGHT",
                    message="Search is already processing. Retry shortly.",
                    quota_remaining=quota_remaining,
                    checks_today=checks_today,
                    tier=tier,
                )
                self._log_completion(
                    request=request,
                    tier=tier,
                    cell_id=cell_id,
                    construction=None,
                    demand=None,
                    quota_remaining=quota_remaining,
                    checks_today=checks_today,
                    payload=response,
                )
                return response

        try:
            coord_key = self._coord_key(request.lat, request.lon)
            candidates = await self._deps.precompute_repo.get_candidates(cell_id)
            logger.debug(
                "search_candidates",
                cell_id=cell_id,
                candidate_count=len(candidates),
                target=request.target.value,
            )

            construction = None
            demand = None
            if request.target in (SearchTarget.CONSTRUCTION, SearchTarget.BOTH):
                construction_score_value, score_source, min_dist_m = await self._compute_construction_score(
                    lat=request.lat,
                    lon=request.lon,
                    cell_id=cell_id,
                    candidates=candidates,
                    target=target,
                )
                construction = GaugeResult(
                    score=construction_score_value,
                    score_source=score_source,
                    coord_key=coord_key,
                    message_code="CONSTRUCTION_READY",
                    message="Construction analysis complete",
                )
                self._record_previous_score(
                    score=construction_score_value,
                    cell_id=cell_id,
                    user_id=user_id,
                    anon_id=anon_id,
                    session_id=session_id,
                    attempt_id=attempt_id,
                    min_dist_m=min_dist_m,
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
                message=ReportRenderer.render(candidates, request.lat, request.lon, limit=1)[0].text
                if candidates
                else "No nearby signals",
                quota_remaining=quota_remaining,
                checks_today=checks_today,
                tier=tier,
            )

            if self._deps.redis:
                try:
                    await self._deps.redis.set(
                        cache_key,
                        response.model_dump_json(),
                        ex=self.CACHE_TTL_SECONDS,
                    )
                except Exception as exc:
                    logger.warning(
                        "search_service_cache_write_failed",
                        error_type=type(exc).__name__,
                        error_detail=str(exc),
                        cache_key=cache_key,
                        description="Failed to write search cache; response still returned.",
                    )
                    sentry_sdk.capture_exception(exc)
            logger.info(
                "search_result_complete",
                construction_score=construction.score if construction else None,
                construction_source=construction.score_source if construction else None,
                demand_score=demand.score if demand else None,
                tier_counts=self._last_tier_counts,
                cell_id=cell_id,
                cache_hit=False,
                target=target,
                tier=tier,
            )
            self._log_completion(
                request=request,
                tier=tier,
                cell_id=cell_id,
                construction=construction,
                demand=demand,
                quota_remaining=quota_remaining,
                checks_today=checks_today,
                payload=response,
            )
            return response
        except Exception as exc:
            logger.error(
                "search_service_compute_failed",
                error_type=type(exc).__name__,
                error_detail=str(exc),
                target=target,
                cell_id=cell_id,
                description="Search compute pipeline failed unexpectedly.",
            )
            sentry_sdk.capture_exception(exc)
            raise
        finally:
            if self._deps.redis and lock_acquired:
                try:
                    await self._deps.redis.delete(lock_key)
                except Exception as exc:
                    logger.warning(
                        "search_service_lock_release_failed",
                        error_type=type(exc).__name__,
                        error_detail=str(exc),
                        lock_key=lock_key,
                        description="Failed to release in-flight search lock.",
                    )
                    sentry_sdk.capture_exception(exc)


    def _record_previous_score(
        self,
        *,
        score: int,
        cell_id: str,
        user_id: str | None,
        anon_id: str | None,
        session_id: str | None,
        attempt_id: str | None,
        min_dist_m: float | None = None,
    ) -> None:
        user_key = user_id or anon_id
        if user_key is None:
            return

        prev = _previous_scores.get(user_key)
        if prev is not None and prev.get("score") == score:
            logger.warning(
                "consecutive_identical_score",
                user_id=user_id,
                anon_id=anon_id,
                session_id=session_id,
                current_score=score,
                previous_score=prev.get("score"),
                current_cell_id=cell_id,
                previous_cell_id=prev.get("cell_id"),
                current_attempt_id=attempt_id,
                previous_attempt_id=prev.get("attempt_id"),
            )
            previous_min_dist = prev.get("min_dist_m")
            if previous_min_dist is not None and min_dist_m is not None:
                distance_difference = abs(float(previous_min_dist) - float(min_dist_m))
                if distance_difference > 10:
                    logger.info(
                        "identical_score_different_distance",
                        score_difference=0,
                        distance_difference=round(distance_difference, 1),
                        detail="Same score, different nearest POI distances — "
                        "ring binning masks proximity differences",
                    )
        _previous_scores[user_key] = {
            "score": score,
            "cell_id": cell_id,
            "attempt_id": attempt_id,
            "min_dist_m": min_dist_m,
        }

    def _log_completion(
        self,
        *,
        request: SearchRequest,
        tier: str,
        cell_id: str,
        construction: GaugeResult | None,
        demand: GaugeResult | None,
        quota_remaining: int,
        checks_today: int,
        payload: SearchResponse,
    ) -> None:
        construction_score_value = construction.score if construction else None
        construction_source = construction.score_source if construction else None
        logger.info(
            "search_service_request_completed",
            target=request.target.value,
            tier=tier,
            cell_id=cell_id,
            construction_score=construction_score_value,
            construction_source=construction_source,
            demand_score=demand.score if demand else None,
            quota_remaining=quota_remaining,
            checks_today=checks_today,
            cached=getattr(payload, "message_code", "") == "CACHE_HIT",
            construction_tier_counts=self._last_tier_counts,
        )
