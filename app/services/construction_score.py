"""Construction confidence scoring — tier-weighted + ambient density.

Formula
-------
1. Tier-weighted component (0-70 points):
   raw_weight = tier_counts[0]*4 + tier_counts[1]*3 + tier_counts[2]*2 + tier_counts[3]*1
   tier_score = min(70, raw_weight * 10)

2. Ambient component (0-30 points):
   If grid_p99 > 0:
       ambient_score = min(30, round(30 * grid_count / grid_p99))
   Else:
       ambient_score = 0

3. Final:
   score = min(100, tier_score + ambient_score)

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
    >>> construction_score([100, 100, 100, 100], 500, 100.0)
    100
    >>> construction_score([5, 0, 0, 0], 0, 0.0)
    70
    >>> construction_score([0, 0, 0, 0], 50, 100.0)
    15
    >>> construction_score([0, 0, 0, 0], 300, 100.0)
    30
    >>> construction_score([0, 0, 0, 0], 0, 50.0)
    0
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

        weights: List[int] = [4, 3, 2, 1]
        raw_weight: int = sum(t * w for t, w in zip(tier_counts, weights))
        tier_score: int = min(70, raw_weight * 10)
        logger.debug(
            "construction_score_tier",
            raw_weight=raw_weight,
            tier_score=tier_score,
            tier_cap_applied=(tier_score < raw_weight * 10),
        )

        if grid_p99 > 0:
            ambient_score: int = min(30, round(30.0 * grid_count / grid_p99))
        else:
            if grid_sample_size == 0:
                logger.debug(
                    "construction_score_grid_data_missing",
                    grid_p99=grid_p99,
                    grid_sample_size=grid_sample_size,
                    detail="Grid percentile sample size is zero; ambient score treated as zero",
                )
            ambient_score = 0
        logger.debug(
            "construction_score_ambient",
            ambient_score=ambient_score,
            grid_p99_zero=(grid_p99 <= 0),
            grid_sample_size=grid_sample_size,
        )

        score = min(100, tier_score + ambient_score)
        logger.info(
            "construction_score_result",
            score=score,
            tier_score=tier_score,
            ambient_score=ambient_score,
            tier_counts=tier_counts,
            grid_count=grid_count,
            grid_p99=grid_p99,
            grid_sample_size=grid_sample_size,
        )

    except (TypeError, ValueError, ArithmeticError) as exc:
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
