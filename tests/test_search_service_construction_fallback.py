import pytest

from app.schemas.search import SearchRequest
from app.services.search_service import SearchDependencies, SearchService


class DummyPrecomputeRepo:
    async def get_candidates(self, cell_id):
        return [object(), object(), object(), object(), object()]


class DummyDemandService:
    async def get_demand_rolling(self, cell_id):
        return 0


class FailingPoiService:
    async def get_construction_distance_bins(self, lat, lon):
        raise RuntimeError("poi backend unavailable")


@pytest.mark.asyncio
async def test_construction_score_falls_back_to_legacy_formula_when_real_compute_fails():
    svc = SearchService(
        SearchDependencies(
            redis=None,
            precompute_repo=DummyPrecomputeRepo(),
            demand_service=DummyDemandService(),
            poi_service=FailingPoiService(),
        )
    )

    res = await svc.run(
        request=SearchRequest(lat=16.048792, lon=108.240859, target="construction"),
        tier="free",
        quota_remaining=1,
        checks_today=0,
    )

    assert res.construction is not None
    assert res.construction.score == 50
    assert res.construction.score_source == "legacy_fallback"
