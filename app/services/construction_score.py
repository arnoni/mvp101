"""Construction confidence scoring — per-ring piecewise-linear + ambient density.

Formula
-------
1. Tier component (0-100 points before overall cap):
   Each distance ring has its own first-POI score, per-POI increment, and hard cap.
   tier_score = sum(_ring_score(tier_counts[i], i) for i in 0..3)

2. Ambient component (0-15 points):
   If grid_p99 > 0:
       ambient_score = min(15, round(15 * grid_count / grid_p99))
   Else:
       ambient_score = 0

3. Final:
   raw_total = tier_score + ambient_score
   final_score = min(100, round(raw_total))

The tier_counts represent COUNT(*) per distance ring measured from the
user's exact raw GPS coordinate. This is computed at query time by
poi_service.get_construction_distance_bins() using ST_DWithin on the
geom geography column (index-driven).

The grid_count is the number of POIs in the user's own 50m x 50m cell
(from cell_poi_stats). The grid_p99 is the 99th percentile of
grid_poi_count across all cells (from cell_poi_percentiles, updated
daily, cached in Redis).
"""

from __future__ import annotations

from typing import List

import sentry_sdk
import structlog

from app.core.analytics import capture

logger = structlog.get_logger(__name__)

# Tier 0 (0-10 m):  first POI = 90, +5 per extra, cap 100
# Tier 1 (10-20 m): first POI = 50, +10 per extra, cap 80
# Tier 2 (20-30 m): first POI = 20, +5 per extra, cap 35
# Tier 3 (30-40 m): first POI =  8, +2 per extra, cap 14
#
# First three values (90, 50, 20, 8) are tuned to match ground-truth
# field data. The increments (5, 10, 5, 2) produce diminishing returns
# per additional POI within the same ring.
TIER_FIRST_SCORE: list[float] = [90.0, 50.0, 20.0, 8.0]
TIER_INCREMENT: list[float] = [5.0, 10.0, 5.0, 2.0]
TIER_MAX: list[float] = [100.0, 80.0, 35.0, 14.0]


def _ring_score(count: int, ring_index: int) -> float:
    """Score for a single ring. Zero count returns 0."""
    if count <= 0:
        return 0.0
    first = TIER_FIRST_SCORE[ring_index]
    incr = TIER_INCREMENT[ring_index]
    max_s = TIER_MAX[ring_index]
    return min(max_s, first + (count - 1) * incr)


def construction_score(
    tier_counts: List[int],
    grid_count: int,
    grid_p99: float,
    grid_sample_size: int = 0,
) -> int:
    """Compute a construction confidence score in the range [0, 100].

    Args:
        tier_counts: [count_0_10km, count_10_20km, count_20_30km, count_30_40km].
            Each value is the COUNT of POIs within that distance ring from the
            user's exact query GPS. Computed at query time.
        grid_count: Total POIs in the user's 50m x 50m cell (from cell_poi_stats).
        grid_p99:   99th percentile of grid_poi_count across all cells
                    (from cell_poi_percentiles, updated daily).
        grid_sample_size: Number of grid percentile samples used to distinguish
                    missing grid data from genuinely zero grid activity.

    Returns:
        Integer score between 0 and 100 inclusive.

    >>> construction_score([0, 0, 0, 0], 0, 0.0)
    0
    >>> construction_score([1, 0, 0, 0], 0, 0.0)
    90
    >>> construction_score([2, 0, 0, 0], 0, 0.0)
    95
    >>> construction_score([3, 0, 0, 0], 0, 0.0)
    100
    >>> construction_score([0, 1, 0, 0], 0, 0.0)
    50
    >>> construction_score([0, 2, 0, 0], 0, 0.0)
    60
    >>> construction_score([0, 3, 0, 0], 0, 0.0)
    70
    >>> construction_score([0, 0, 1, 0], 0, 0.0)
    20
    >>> construction_score([0, 0, 0, 1], 0, 0.0)
    8
    >>> construction_score([100, 100, 100, 100], 500, 100.0)
    100
    >>> construction_score([0, 0, 0, 0], 50, 100.0)
    8
    >>> construction_score([0, 1, 0, 0], 50, 100.0)
    58
    """
    logger.debug(
        "construction_score_input",
        tier_counts=tier_counts,
        grid_count=grid_count,
        grid_p99=grid_p99,
        grid_sample_size=grid_sample_size,
    )

    if (
        not isinstance(tier_counts, list)
        or len(tier_counts) != 4
        or not all(isinstance(count, int) for count in tier_counts)
    ):
        logger.warning(
            "construction_score_invalid_input",
            detail="tier_counts must be a list of 4 integers",
            received_type=type(tier_counts).__name__,
            received_length=len(tier_counts) if isinstance(tier_counts, list) else None,
        )
        sentry_sdk.capture_message(
            f"construction_score called with invalid tier_counts: {type(tier_counts).__name__}",
            level="warning",
        )
        tier_counts = (
            list(tier_counts)[:4]
            if isinstance(tier_counts, (list, tuple))
            else [0, 0, 0, 0]
        )
        while len(tier_counts) < 4:
            tier_counts.append(0)

    score = 0
    tier_score = 0.0
    ambient_score = 0
    raw_total = 0.0
    ring_scores: list[float] = [0.0, 0.0, 0.0, 0.0]
    ring_breakdown: list[dict[str, float | int]] = []

    try:
        if grid_count < 0:
            logger.warning("construction_score_clamping_grid_count", grid_count=grid_count)
            sentry_sdk.capture_message(
                f"construction_score received negative grid_count={grid_count}",
                level="warning",
            )
            grid_count = 0

        if grid_p99 < 0:
            logger.warning("construction_score_clamping_grid_p99", grid_p99=grid_p99)
            sentry_sdk.capture_message(
                f"construction_score received negative grid_p99={grid_p99}",
                level="warning",
            )
            grid_p99 = 0.0

        if all(c == 0 for c in tier_counts) and grid_count == 0:
            logger.warning(
                "construction_score_all_zero_input",
                tier_counts=tier_counts,
                grid_count=grid_count,
                grid_p99=grid_p99,
                grid_sample_size=grid_sample_size,
                detail="All tier counts and grid count are zero — upstream data may be missing "
                "rather than area genuinely empty",
            )

        for ring_index, count in enumerate(tier_counts):
            ring_score = _ring_score(count, ring_index)
            ring_scores[ring_index] = ring_score
            breakdown = {
                "ring_index": ring_index,
                "count": count,
                "first_score": TIER_FIRST_SCORE[ring_index],
                "increment": TIER_INCREMENT[ring_index],
                "ring_max": TIER_MAX[ring_index],
                "ring_score": ring_score,
            }
            ring_breakdown.append(breakdown)
            logger.debug("construction_score_ring_breakdown", **breakdown)

        tier_score = sum(ring_scores)
        logger.debug(
            "construction_score_tier",
            tier_score=tier_score,
            ring_scores=ring_scores,
        )

        if grid_p99 > 0:
            ambient_score = min(15, round(15.0 * grid_count / grid_p99))
        else:
            if grid_sample_size == 0:
                logger.warning(
                    "construction_score_grid_data_missing",
                    grid_p99=grid_p99,
                    grid_sample_size=grid_sample_size,
                    detail="Grid percentile sample size is zero; ambient score treated as zero",
                )
            ambient_score = 0
        logger.debug(
            "construction_score_ambient",
            grid_count=grid_count,
            grid_p99=grid_p99,
            ambient_score=ambient_score,
            grid_sample_size=grid_sample_size,
        )

        raw_total = tier_score + ambient_score
        score = min(100, round(raw_total))
        logger.info(
            "construction_score_result",
            tier_score=tier_score,
            ambient_score=ambient_score,
            raw_total=raw_total,
            final_score=score,
            score=score,
            was_capped=(raw_total > 100),
            ring_scores=ring_scores,
            ring_breakdown=ring_breakdown,
            tier_counts=tier_counts,
            grid_count=grid_count,
            grid_p99=grid_p99,
            grid_sample_size=grid_sample_size,
        )

    except (TypeError, ValueError, ArithmeticError, ZeroDivisionError) as exc:
        logger.error(
            "construction_score_computation_failed",
            error_type=type(exc).__name__,
            error_detail=str(exc),
            tier_counts=tier_counts,
            grid_count=grid_count,
            grid_p99=grid_p99,
            grid_sample_size=grid_sample_size,
        )
        sentry_sdk.capture_exception(exc)
        score = 0

    capture(
        user_id="system",
        event="construction_score_computed",
        properties={
            "score": score,
            "tier_score": tier_score,
            "ambient_score": ambient_score,
            "ring_scores": ring_scores,
            "tier_counts": tier_counts,
            "grid_count": grid_count,
            "grid_p99": grid_p99,
            "grid_sample_size": grid_sample_size,
            "source": "pure_function",
        },
    )
    return score


if __name__ == "__main__":
    import doctest

    doctest.testmod()
