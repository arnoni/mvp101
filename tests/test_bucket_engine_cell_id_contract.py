from pathlib import Path


def test_bucket_engine_cell_id_uses_lat_lon_order_and_fixed_precision():
    bucket_engine_py = Path("app/services/bucket_engine.py").read_text(encoding="utf-8")
    assert "def get_cell_id(lat: float, lon: float) -> str" in bucket_engine_py
    assert 'return f"{cell_lat:.{BucketEngine.CELL_ID_DECIMALS}f},{cell_lon:.{BucketEngine.CELL_ID_DECIMALS}f}"' in bucket_engine_py


def test_search_route_computes_demand_cell_id_from_query_coordinates():
    routes_py = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert "demand_cell_id = BucketEngine.get_cell_id(data.lat, data.lon)" in routes_py
    assert 'logger.info("demand_cell_id_computed", lat=data.lat, lon=data.lon, demand_cell_id=demand_cell_id)' in routes_py
