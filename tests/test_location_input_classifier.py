import pytest

from app.services.location_input_classifier import classify_location_input


def test_empty_string():
    c = classify_location_input("")
    assert c.input_format == "empty"
    assert c.parse_status == "empty"


def test_whitespace_string():
    c = classify_location_input("   ")
    assert c.input_format == "empty"
    assert c.parse_status == "empty"


@pytest.mark.parametrize(
    "value",
    [
        "16.0199444 108.2548611",
        "16.0199444,108.2548611",
        "16.0199444, 108.2548611",
        "lat: 16.0199444 lng: 108.2548611",
    ],
)
def test_decimal_coordinates_variants(value: str):
    c = classify_location_input(value)
    assert c.input_format == "decimal_coordinates"
    assert c.parse_status == "parsed"
    assert c.has_coordinates is True
    assert c.normalized_lat == pytest.approx(16.0199444)
    assert c.normalized_lng == pytest.approx(108.2548611)


def test_dms_coordinates():
    c = classify_location_input("16°01'11.8\"N 108°15'17.5\"E")
    assert c.input_format == "dms_coordinates"
    assert c.parse_status == "parsed"
    assert c.normalized_lat == pytest.approx(16.0199444, abs=1e-4)
    assert c.normalized_lng == pytest.approx(108.2548611, abs=1e-4)


def test_dm_coordinates():
    c = classify_location_input("16°01.198'N 108°15.292'E")
    assert c.input_format == "dm_coordinates"
    assert c.parse_status == "parsed"


def test_google_maps_at_url():
    c = classify_location_input("https://www.google.com/maps/@16.0199444,108.2548611,18z")
    assert c.input_format == "google_maps_at_url"
    assert c.parse_status == "parsed"
    assert c.input_host in {"www.google.com", "google.com"}
    assert c.coordinate_source == "google_maps_at_segment"


def test_google_maps_place_url_3d4d_tokens():
    c = classify_location_input("https://www.google.com/maps/place/test/data=!3m3!8m2!3d16.0199444!4d108.2548611")
    assert c.input_format in {"google_maps_place_url", "google_maps_full_url"}
    assert c.parse_status == "parsed"
    assert c.coordinate_source == "google_maps_3d4d_tokens"


def test_maps_app_google_short_url():
    c = classify_location_input("https://maps.app.goo.gl/QEWSpGGS7iLZomR98")
    assert c.input_format == "google_maps_short_url"
    assert c.parse_status == "recognized_not_resolved"
    assert c.input_host == "maps.app.goo.gl"
    assert c.has_coordinates is False
    assert c.failure_reason == "short_url_not_resolved"


def test_googl_maps_short_url():
    c = classify_location_input("https://goo.gl/maps/abc123")
    assert c.input_format == "google_maps_short_url"
    assert c.parse_status == "recognized_not_resolved"


def test_geo_uri():
    c = classify_location_input("geo:16.0199444,108.2548611")
    assert c.input_format == "geo_uri"
    assert c.parse_status == "parsed"


def test_apple_maps_url():
    c = classify_location_input("https://maps.apple.com/?ll=16.0199444,108.2548611")
    assert c.input_format == "apple_maps_url"
    assert c.parse_status == "parsed"
    assert c.coordinate_source == "apple_maps_ll_param"


def test_osm_url():
    c = classify_location_input("https://www.openstreetmap.org/?mlat=16.0199444&mlon=108.2548611")
    assert c.input_format == "openstreetmap_url"
    assert c.parse_status == "parsed"
    assert c.coordinate_source == "osm_mlat_mlon_params"


def test_waze_url():
    c = classify_location_input("https://waze.com/ul?ll=16.0199444,108.2548611")
    assert c.input_format == "waze_url"
    assert c.parse_status == "parsed"
    assert c.coordinate_source == "waze_ll_param"


def test_plain_address():
    c = classify_location_input("23 Phước Trường 10, An Hải, Sơn Trà, Đà Nẵng")
    assert c.input_format == "plain_address"
    assert c.parse_status == "recognized_unsupported"
    assert c.failure_reason == "plain_address_unsupported"


def test_unknown_url():
    c = classify_location_input("https://example.com/some-location")
    assert c.input_format == "unknown_url"
    assert c.parse_status == "unknown"


def test_mixed_text_with_decimal_coordinates():
    c = classify_location_input("please check this place 16.0199444 108.2548611 thanks")
    assert c.input_format == "mixed_text_with_coordinates"
    assert c.parse_status == "parsed"
    assert c.coordinate_source == "mixed_text_decimal"


def test_invalid_coordinate_range():
    c = classify_location_input("999 999")
    assert c.parse_status == "invalid_coordinates"
    assert c.failure_reason == "invalid_coordinate_range"
