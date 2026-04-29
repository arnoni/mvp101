from pathlib import Path


def test_search_route_defines_demand_cell_id_before_recording_query():
    routes_py = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert "demand_cell_id = BucketEngine.get_cell_id(data.lat, data.lon)" in routes_py
    assert "await demand_service.record_query(" in routes_py
    assert "demand_cell_id," in routes_py
    assert 'logger.warning("demand_record_query_failed", cell_id=demand_cell_id' in routes_py


def test_search_route_uses_hourly_actor_dedupe_for_demand_recording():
    routes_py = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert "demand_actor_key = str(user_id or request.cookies.get(settings.SESSION_COOKIE_NAME) or anon_id or \"unknown\")" in routes_py
    assert "dedupe_window_seconds=3600" in routes_py
    assert "demand_record_query_deduped" in routes_py
