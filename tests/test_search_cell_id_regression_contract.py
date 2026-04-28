from pathlib import Path


def test_search_route_defines_demand_cell_id_before_recording_query():
    routes_py = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert "demand_cell_id = BucketEngine.get_cell_id(data.lat, data.lon)" in routes_py
    assert "await demand_service.record_query(demand_cell_id)" in routes_py
    assert 'logger.warning("demand_record_query_failed", cell_id=demand_cell_id' in routes_py
