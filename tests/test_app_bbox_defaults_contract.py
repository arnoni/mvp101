from pathlib import Path


def test_default_app_bbox_is_expanded_for_reported_coordinate():
    config_py = Path("app/core/config.py").read_text(encoding="utf-8")
    assert "[108.05, 15.85, 108.35, 16.20]" in config_py
    assert "DA_NANG_BBOX: List[float] = Field(" in config_py
    assert "APP_BOUNDING_BOX: List[float] = Field(" in config_py
