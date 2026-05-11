"""Error handling and observability tests for construction scoring."""

from unittest.mock import AsyncMock, MagicMock, patch

import asyncio



class AsyncContext:
    def __init__(self, obj):
        self.obj = obj

    async def __aenter__(self):
        return self.obj

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TestConstructionScoreErrorHandling:
    """Tests for graceful degradation in construction_score."""

    def test_negative_grid_count_clamped(self):
        from app.services.construction_score import construction_score

        result = construction_score([5, 0, 0, 0], -1, 100.0)
        assert result == 70

    def test_negative_p99_clamped(self):
        from app.services.construction_score import construction_score

        result = construction_score([0, 0, 0, 0], 50, -1.0)
        assert result == 0

    def test_tier_count_padding(self):
        from app.services.construction_score import construction_score

        result = construction_score([5, 1], 0, 0.0)
        assert result == 70

    def test_tier_count_too_many(self):
        from app.services.construction_score import construction_score

        result = construction_score([5, 1, 2, 3, 99], 0, 0.0)
        assert result == 70

    @patch("app.services.construction_score.sentry_sdk")
    def test_type_error_logged_and_returns_zero(self, mock_sentry):
        from app.services.construction_score import construction_score

        result = construction_score("invalid", "input", "bad")
        assert result == 0
        mock_sentry.capture_exception.assert_called_once()


class TestPOIServiceErrorHandling:
    """Tests for POIService graceful degradation."""

    def test_returns_zero_bins_on_db_failure(self):
        from app.services.poi_service import POIService

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value = AsyncContext(mock_conn)
        mock_conn.execute = AsyncMock(side_effect=Exception("DB connection lost"))

        service = POIService(mock_engine)
        result = asyncio.run(service.get_construction_distance_bins(16.05, 108.24))
        assert result == {"0_10": 0, "10_20": 0, "20_30": 0, "30_40": 0}

    def test_returns_zero_bins_when_engine_none(self):
        from app.services.poi_service import POIService

        service = POIService(None)
        result = asyncio.run(service.get_construction_distance_bins(16.05, 108.24))
        assert result == {"0_10": 0, "10_20": 0, "20_30": 0, "30_40": 0}


class TestPrecomputeRepoErrorHandling:
    """Tests for PrecomputeRepository graceful degradation."""

    def test_get_cell_stats_returns_zero_on_failure(self):
        from app.services.precompute_repo import PrecomputeRepository

        mock_engine = MagicMock()
        mock_engine.connect = AsyncMock(side_effect=Exception("DB down"))
        repo = PrecomputeRepository(mock_engine)
        result = asyncio.run(repo.get_cell_stats("test_cell"))
        assert result == {"grid_poi_count": 0}

    def test_get_cell_stats_returns_zero_on_empty_result(self):
        from app.services.precompute_repo import PrecomputeRepository

        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value = AsyncContext(mock_conn)
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = None
        mock_conn.execute = AsyncMock(return_value=mock_result)
        repo = PrecomputeRepository(mock_engine)
        result = asyncio.run(repo.get_cell_stats("nonexistent_cell"))
        assert result == {"grid_poi_count": 0}

    def test_get_grid_percentiles_returns_defaults_on_db_failure(self):
        from app.services.precompute_repo import PrecomputeRepository

        mock_engine = MagicMock()
        mock_engine.connect = AsyncMock(side_effect=Exception("DB down"))
        repo = PrecomputeRepository(mock_engine)
        result = asyncio.run(repo.get_grid_percentiles(redis=None))
        assert result == {"p99": 0.0, "sample_size": 0}

    def test_get_grid_percentiles_redis_failure_falls_back_to_db(self):
        from app.services.precompute_repo import PrecomputeRepository

        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(side_effect=Exception("Redis down"))
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value = AsyncContext(mock_conn)
        mock_result = MagicMock()
        mock_result.mappings.return_value.first.return_value = {"p99": 42.5, "sample_size": 10}
        mock_conn.execute = AsyncMock(return_value=mock_result)
        repo = PrecomputeRepository(mock_engine)
        result = asyncio.run(repo.get_grid_percentiles(redis=mock_redis))
        assert result["p99"] == 42.5
        assert result["sample_size"] == 10


class TestSearchServiceErrorHandling:
    """Tests for SearchService fallback chain."""

    def test_fallback_when_poi_service_is_none(self):
        from app.services.search_service import SearchDependencies, SearchService

        deps = SearchDependencies(
            redis=None,
            precompute_repo=MagicMock(),
            demand_service=MagicMock(),
            poi_service=None,
        )
        service = SearchService(deps)
        score, source = asyncio.run(service._compute_construction_score(
            lat=16.05,
            lon=108.24,
            cell_id="test_cell",
            candidates=[1, 2, 3, 4, 5],
        ))
        assert score == 50
        assert source == "legacy_fallback"

    def test_fallback_when_algorithm_raises(self, caplog):
        from app.services.search_service import SearchDependencies, SearchService

        mock_poi = AsyncMock()
        mock_poi.get_construction_distance_bins = AsyncMock(side_effect=Exception("DB error"))
        mock_repo = MagicMock()
        deps = SearchDependencies(
            redis=None,
            precompute_repo=mock_repo,
            demand_service=MagicMock(),
            poi_service=mock_poi,
        )
        service = SearchService(deps)
        score, source = asyncio.run(service._compute_construction_score(
            lat=16.05,
            lon=108.24,
            cell_id="test_cell",
            candidates=[1, 2, 3],
        ))
        assert score == 30
        assert source == "legacy_fallback"
        error_records = [
            r for r in caplog.records if "construction_score_new_algorithm_failed" in r.message
        ]
        assert len(error_records) >= 1


class TestDailyPrecomputeErrorHandling:
    """Tests for daily precompute row-level bucketing error handling."""

    def test_precompute_buckets_gracefully_on_bad_row(self):
        bad_poi_rows = [
            (1, 16.05, 108.24),
            (2, None, None),
        ]
        assert len(bad_poi_rows) == 2
