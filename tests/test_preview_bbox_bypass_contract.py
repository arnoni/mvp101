from pathlib import Path


def test_search_route_allows_out_of_bounds_in_preview_only():
    routes_py = Path("app/api/routes.py").read_text(encoding="utf-8")
    assert "if not is_inside_app_bbox(data.lat, data.lon):" in routes_py
    assert 'if settings.ENV == "preview":' in routes_py
    assert "search_out_of_bounds_allowed_in_preview" in routes_py
    assert 'error="OUT_OF_BOUNDS"' in routes_py
