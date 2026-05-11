from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.dto import PrecomputeCandidate
from app.models.models import POI


def test_precompute_candidate_accepts_new_poi_columns_null_and_populated_values():
    null_candidate = PrecomputeCandidate(
        id="1",
        lat=16.047079,
        lon=108.206230,
        category="construction",
        name="Null field construction site",
        activity_status=None,
        noise_level=None,
        expected_time_to_complete=None,
    )
    assert null_candidate.activity_status is None
    assert null_candidate.noise_level is None
    assert null_candidate.expected_time_to_complete is None

    zero_noise_candidate = PrecomputeCandidate(
        id="2",
        lat=16.047079,
        lon=108.206230,
        category="construction",
        activity_status="active",
        noise_level=0,
        expected_time_to_complete="2026-09-01",
    )
    assert zero_noise_candidate.activity_status == "active"
    assert zero_noise_candidate.noise_level == 0
    assert zero_noise_candidate.expected_time_to_complete == date(2026, 9, 1)
    assert (
        zero_noise_candidate.model_dump(mode="json")["expected_time_to_complete"]
        == "2026-09-01"
    )

    max_noise_candidate = PrecomputeCandidate(
        id="3",
        lat=16.047079,
        lon=108.206230,
        category="construction",
        noise_level=100,
    )
    assert max_noise_candidate.noise_level == 100


@pytest.mark.parametrize("noise_level", [-1, 101])
def test_precompute_candidate_rejects_noise_level_outside_database_range(noise_level):
    with pytest.raises(ValidationError):
        PrecomputeCandidate(
            id="rogue",
            lat=16.047079,
            lon=108.206230,
            category="construction",
            noise_level=noise_level,
        )


def test_precompute_candidate_rejects_unknown_activity_status():
    with pytest.raises(ValidationError):
        PrecomputeCandidate(
            id="rogue-status",
            lat=16.047079,
            lon=108.206230,
            category="construction",
            activity_status="unknown",
        )


def test_poi_orm_model_maps_new_columns_as_nullable():
    assert POI.__table__.c.noise_level.nullable is True
    assert POI.__table__.c.expected_time_to_complete.nullable is True
    assert POI.__table__.c.activity_status.nullable is True


def test_daily_precompute_fetch_includes_new_public_poi_columns():
    source = Path("jobs/daily_precompute.py").read_text()
    assert "activity_status" in source
    assert "noise_level" in source
    assert "expected_time_to_complete" in source
