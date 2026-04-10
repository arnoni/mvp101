import pytest

from app.services.location_parser import (
    MalformedLocationInputError,
    ShortUrlResolutionError,
    UnsupportedLocationInputError,
    parse_decimal_pair,
    parse_degree_pair,
    parse_google_maps_url,
    parse_location_input,
    resolve_google_maps_short_url,
)


def test_parse_decimal_pair_comma():
    parsed = parse_decimal_pair("16.0544, 108.2022")
    assert parsed.latitude == pytest.approx(16.0544)
    assert parsed.longitude == pytest.approx(108.2022)


def test_parse_decimal_pair_space():
    parsed = parse_decimal_pair("16.0544 108.2022")
    assert parsed.normalized_input == "16.054400, 108.202200"


def test_parse_decimal_pair_reversed_order_autonormalized():
    parsed = parse_decimal_pair("108.2022,16.0544")
    assert parsed.latitude == pytest.approx(16.0544)
    assert parsed.longitude == pytest.approx(108.2022)
    assert parsed.resolution_method == "decimal_pair_reversed"


def test_parse_invalid_range_raises():
    with pytest.raises(Exception):
        parse_decimal_pair("160.0000, 108.2022")


def test_parse_degree_pair_valid():
    parsed = parse_degree_pair("16°03'15.8\"n 108°12'07.9\"e")
    assert parsed.latitude == pytest.approx(16.054388, rel=1e-4)
    assert parsed.longitude == pytest.approx(108.202194, rel=1e-4)


def test_parse_degree_missing_hemisphere():
    with pytest.raises(MalformedLocationInputError):
        parse_degree_pair("16°03'15.8\" 108°12'07.9\"E")


def test_parse_google_long_prefers_place_coordinates():
    url = "https://www.google.com/maps/place/x/@16.1,108.1,17z/data=!3d16.0544!4d108.2022"
    parsed = parse_google_maps_url(url)
    assert parsed.latitude == pytest.approx(16.0544)
    assert parsed.longitude == pytest.approx(108.2022)


def test_parse_google_long_viewport_fallback():
    url = "https://www.google.com/maps/place/x/@16.0544,108.2022,17z"
    parsed = parse_google_maps_url(url)
    assert parsed.resolution_method == "viewport_center"


def test_parse_unsupported_domain_rejected():
    with pytest.raises(UnsupportedLocationInputError):
        parse_location_input("https://example.com/?q=16.0544,108.2022")


def test_parse_google_link_no_coordinates_rejected():
    with pytest.raises(MalformedLocationInputError):
        parse_google_maps_url("https://www.google.com/maps")


def test_short_url_resolution_timeout(monkeypatch):
    import httpx

    class RaisingClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: RaisingClient())
    with pytest.raises(ShortUrlResolutionError):
        resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")


@pytest.mark.parametrize(
    "url",
    [
        "https://goo.gl/maps/abc123",
        "https://g.page/some-place",
    ],
)
def test_parse_google_supported_short_hosts(url, monkeypatch):
    monkeypatch.setattr(
        "app.services.location_parser.resolve_google_maps_short_url",
        lambda _: "https://www.google.com/maps/place/x/@16.0544,108.2022,17z",
    )
    parsed = parse_google_maps_url(url)
    assert parsed.input_kind == "google_maps_short_url"
    assert parsed.latitude == pytest.approx(16.0544)
    assert parsed.longitude == pytest.approx(108.2022)


def test_parse_google_invalid_goo_gl_short_path_rejected():
    with pytest.raises(UnsupportedLocationInputError):
        parse_google_maps_url("https://goo.gl/not-maps")
