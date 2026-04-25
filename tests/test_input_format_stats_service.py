from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.services.input_format_stats_service import increment_input_format_stats


@dataclass
class Row:
    count: int
    first_seen_at: datetime
    last_seen_at: datetime


class FakeDB:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, str, str], Row] = {}

    async def execute(self, _sql, params):
        key = (
            params["target_mode"],
            params["input_format"],
            params["input_parse_status"],
            params["input_host"],
            params["user_state"],
        )
        now = datetime.now(timezone.utc)
        existing = self.rows.get(key)
        if existing is None:
            self.rows[key] = Row(count=1, first_seen_at=now, last_seen_at=now)
        else:
            existing.count += 1
            existing.last_seen_at = now


@pytest.mark.asyncio
async def test_new_counter_row_created():
    db = FakeDB()
    await increment_input_format_stats(
        db,
        target_mode="construction",
        input_format="decimal_coordinates",
        input_parse_status="parsed",
        input_host=None,
        user_state="anonymous",
    )
    key = ("construction", "decimal_coordinates", "parsed", "none", "anonymous")
    assert db.rows[key].count == 1


@pytest.mark.asyncio
async def test_existing_row_increments():
    db = FakeDB()
    for _ in range(2):
        await increment_input_format_stats(
            db,
            target_mode="construction",
            input_format="decimal_coordinates",
            input_parse_status="parsed",
            input_host=None,
            user_state="anonymous",
        )
    key = ("construction", "decimal_coordinates", "parsed", "none", "anonymous")
    assert db.rows[key].count == 2


@pytest.mark.asyncio
async def test_separate_rows_for_dimensions_and_host_normalization():
    db = FakeDB()

    await increment_input_format_stats(db, target_mode="construction", input_format="decimal_coordinates", input_parse_status="parsed", input_host=None, user_state="anonymous")
    await increment_input_format_stats(db, target_mode="construction", input_format="google_maps_short_url", input_parse_status="recognized_not_resolved", input_host="maps.app.goo.gl", user_state="anonymous")
    await increment_input_format_stats(db, target_mode="demand", input_format="decimal_coordinates", input_parse_status="parsed", input_host="www.google.com", user_state="anonymous")
    await increment_input_format_stats(db, target_mode="demand", input_format="decimal_coordinates", input_parse_status="recognized_not_resolved", input_host="maps.google.com", user_state="anonymous")
    await increment_input_format_stats(db, target_mode="demand", input_format="decimal_coordinates", input_parse_status="parsed", input_host="www.google.com", user_state="registered")
    await increment_input_format_stats(db, target_mode="construction", input_format="unknown_url", input_parse_status="unknown", input_host="example.com", user_state="anonymous")
    await increment_input_format_stats(db, target_mode="construction", input_format="openstreetmap_url", input_parse_status="parsed", input_host="www.openstreetmap.org", user_state="anonymous")
    await increment_input_format_stats(db, target_mode="construction", input_format="waze_url", input_parse_status="parsed", input_host="www.waze.com", user_state="anonymous")

    assert ("construction", "decimal_coordinates", "parsed", "none", "anonymous") in db.rows
    assert ("construction", "google_maps_short_url", "recognized_not_resolved", "maps.app.goo.gl", "anonymous") in db.rows
    assert ("demand", "decimal_coordinates", "parsed", "google.com", "anonymous") in db.rows
    assert ("demand", "decimal_coordinates", "recognized_not_resolved", "google.com", "anonymous") in db.rows
    assert ("demand", "decimal_coordinates", "parsed", "google.com", "registered") in db.rows
    assert ("construction", "unknown_url", "unknown", "other", "anonymous") in db.rows
    assert ("construction", "openstreetmap_url", "parsed", "openstreetmap.org", "anonymous") in db.rows
    assert ("construction", "waze_url", "parsed", "waze.com", "anonymous") in db.rows


@pytest.mark.asyncio
async def test_last_seen_at_updates_on_increment():
    db = FakeDB()
    await increment_input_format_stats(
        db,
        target_mode="construction",
        input_format="decimal_coordinates",
        input_parse_status="parsed",
        input_host=None,
        user_state="anonymous",
    )
    key = ("construction", "decimal_coordinates", "parsed", "none", "anonymous")
    first = db.rows[key]
    first_seen = first.first_seen_at
    first_last = first.last_seen_at

    await increment_input_format_stats(
        db,
        target_mode="construction",
        input_format="decimal_coordinates",
        input_parse_status="parsed",
        input_host=None,
        user_state="anonymous",
    )

    second = db.rows[key]
    assert second.last_seen_at >= second.first_seen_at
    assert second.first_seen_at == first_seen
    assert second.last_seen_at >= first_last
