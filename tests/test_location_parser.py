import sys
import types
import pytest
from unittest.mock import Mock

try:
    import httpx  # noqa: F401
except ImportError:
    httpx_stub = types.ModuleType("httpx")

    class _TimeoutException(Exception):
        pass

    class _RequestError(Exception):
        pass

    class _HTTPStatusError(Exception):
        def __init__(self, message, request=None, response=None):
            super().__init__(message)
            self.request = request
            self.response = response

    class _Client:  # pragma: no cover - tests monkeypatch client behavior directly
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            raise NotImplementedError("Stub httpx.Client.get should be monkeypatched in tests.")

    httpx_stub.TimeoutException = _TimeoutException
    httpx_stub.RequestError = _RequestError
    httpx_stub.HTTPStatusError = _HTTPStatusError
    httpx_stub.Client = _Client
    sys.modules["httpx"] = httpx_stub

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


def test_parse_google_long_query_fallback():
    url = "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022"
    parsed = parse_google_maps_url(url)
    assert parsed.resolution_method == "query_query"
    assert parsed.latitude == pytest.approx(16.0544)
    assert parsed.longitude == pytest.approx(108.2022)


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


def test_short_url_resolution_request_error(monkeypatch):
    import httpx

    class RaisingClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            raise httpx.RequestError("network down")

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: RaisingClient())
    with pytest.raises(ShortUrlResolutionError, match="network/protocol error"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")


def test_short_url_resolution_rejects_non_short_host():
    with pytest.raises(UnsupportedLocationInputError):
        resolve_google_maps_short_url("https://www.google.com/maps/place/x/@16.0544,108.2022,17z")


def test_short_url_resolution_http_error_status(monkeypatch):
    import httpx

    class StubUrl:
        host = "maps.app.goo.gl"

        def __str__(self):
            return "https://maps.app.goo.gl/abc"

    class StubResponse:
        status_code = 503
        url = StubUrl()
        headers = {}
        is_redirect = False
        is_informational = False

        def raise_for_status(self):
            raise httpx.HTTPStatusError("bad status", request=None, response=self)

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return StubResponse()

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    with pytest.raises(ShortUrlResolutionError, match="HTTP 503.*Event details: stage=http_error_status"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")


def test_short_url_resolution_redirect_302_does_not_raise_for_status(monkeypatch):
    import httpx

    class StubResponse:
        def __init__(self, status_code, url, location=None, fail_raise=False):
            self.status_code = status_code
            self.url = url
            self.headers = {"location": location} if location else {}
            self.is_redirect = status_code in {301, 302, 303, 307, 308}
            self.is_informational = 100 <= status_code < 200
            self._fail_raise = fail_raise

        def raise_for_status(self):
            if self._fail_raise:
                raise httpx.HTTPStatusError("redirect", request=None, response=self)
            return None

    class StubClient:
        def __enter__(self):
            self.calls = 0
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            self.calls += 1
            if self.calls == 1:
                return StubResponse(
                    302,
                    "https://maps.app.goo.gl/abc",
                    "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022",
                    fail_raise=True,
                )
            return StubResponse(200, "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022")

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    resolved = resolve_google_maps_short_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")
    assert resolved == "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022"


def test_short_url_resolution_redirect_missing_location_includes_event_details(monkeypatch):
    response = Mock()
    response.status_code = 302
    response.is_redirect = True
    response.is_informational = False
    response.headers = {}
    response.url = "https://maps.app.goo.gl/abc"

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    with pytest.raises(ShortUrlResolutionError, match="stage=redirect_missing_location"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")


def test_short_url_resolution_redirect_chain_success(monkeypatch):
    first_response = Mock()
    first_response.status_code = 302
    first_response.is_redirect = True
    first_response.is_informational = False
    first_response.headers = {"location": "https://www.google.com/maps/place/x/@16.0544,108.2022,17z"}
    first_response.url = "https://maps.app.goo.gl/abc"
    first_response.raise_for_status.return_value = None

    second_response = Mock()
    second_response.status_code = 200
    second_response.is_redirect = False
    second_response.is_informational = False
    second_response.headers = {}
    second_response.url = "https://www.google.com/maps/place/x/@16.0544,108.2022,17z"
    second_response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            self.calls = 0
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            self.calls += 1
            return first_response if self.calls == 1 else second_response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    resolved = resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")
    assert resolved == "https://www.google.com/maps/place/x/@16.0544,108.2022,17z"


def test_short_url_resolution_skips_html_fallback_when_final_url_is_parseable(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.is_redirect = False
    response.is_informational = False
    response.headers = {}
    response.url = "https://www.google.com/maps/place/x/@16.0544,108.2022,17z"
    response.text = '<html><body>"lat":0.0000,"lng":0.0000</body></html>'
    response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    extractor = Mock(side_effect=AssertionError("html fallback should not run for parseable final urls"))
    monkeypatch.setattr("app.services.location_parser._extract_lat_lng_from_google_maps_html", extractor)

    resolved = resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")
    assert resolved == "https://www.google.com/maps/place/x/@16.0544,108.2022,17z"
    extractor.assert_not_called()


def test_short_url_resolution_extracts_coords_from_html_when_final_url_lacks_them(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.is_redirect = False
    response.is_informational = False
    response.headers = {}
    response.url = "https://www.google.com/maps/place/Some+Place/"
    response.text = '<html><head></head><body>"lat":16.0544,"lng":108.2022</body></html>'
    response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    resolved = resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")
    assert resolved == "https://www.google.com/maps/search/?api=1&query=16.0544,108.2022"


def test_short_url_resolution_extracts_percent_encoded_query_pair_from_html(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.is_redirect = False
    response.is_informational = False
    response.headers = {}
    response.url = "https://www.google.com/maps/place/KIM+Sui+cao/data=!4m2!3m1!1s0x3142170067a2441b:0x73a9445ab66c1679!18m1!1e1"
    response.text = '<meta property="og:image" content="https://maps.googleapis.com/maps/api/staticmap?center=16.0544%2C108.2022&zoom=15">'
    response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    resolved = resolve_google_maps_short_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")
    assert resolved == "https://www.google.com/maps/search/?api=1&query=16.0544,108.2022"


def test_short_url_resolution_rejects_ashburn_block_page_coordinates(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.is_redirect = False
    response.is_informational = False
    response.headers = {}
    response.url = "https://www.google.com/maps/place/KIM+Sui+cao/"
    response.text = '<meta property="og:image" content="https://maps.googleapis.com/maps/api/staticmap?center=39.026799%2C-77.844326&zoom=15">'
    response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    with pytest.raises(MalformedLocationInputError, match="blocked/bot-protection"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")


def test_short_url_resolution_uses_native_http_redirect_following(monkeypatch):
    captured_kwargs = {}

    class StubResponse:
        status_code = 200
        is_redirect = False
        is_informational = False
        headers = {}
        url = "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022"
        text = ""

        def raise_for_status(self):
            return None

    class StubClient:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return StubResponse()

    monkeypatch.setattr("app.services.location_parser.httpx.Client", StubClient)
    resolved = resolve_google_maps_short_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")
    assert captured_kwargs["follow_redirects"] is True
    assert resolved == "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022"


@pytest.mark.parametrize("blocked_url", ["http://127.0.0.1/internal", "http://169.254.10.20/latest", "http://10.0.0.5/admin"])
def test_short_url_resolution_redirect_chain_blocks_private_hosts(monkeypatch, blocked_url):
    first_response = Mock()
    first_response.status_code = 302
    first_response.is_redirect = True
    first_response.is_informational = False
    first_response.headers = {"location": blocked_url}
    first_response.url = "https://maps.app.goo.gl/abc"
    first_response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return first_response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    with pytest.raises(ShortUrlResolutionError, match="private or local host"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")


def test_short_url_resolution_redirect_chain_blocks_non_google_final_host(monkeypatch):
    first_response = Mock()
    first_response.status_code = 302
    first_response.is_redirect = True
    first_response.is_informational = False
    first_response.headers = {"location": "https://example.com/maps/place/x/@16.0544,108.2022,17z"}
    first_response.url = "https://maps.app.goo.gl/abc"
    first_response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return first_response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    with pytest.raises(ShortUrlResolutionError, match="unsupported domain"):
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


def test_parse_google_short_url_resolved_query_link(monkeypatch):
    monkeypatch.setattr(
        "app.services.location_parser.resolve_google_maps_short_url",
        lambda _: "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022",
    )
    parsed = parse_google_maps_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")
    assert parsed.input_kind == "google_maps_short_url"
    assert parsed.latitude == pytest.approx(16.0544)
    assert parsed.longitude == pytest.approx(108.2022)


def test_parse_google_short_url_failed_extract_includes_event_details(monkeypatch):
    monkeypatch.setattr(
        "app.services.location_parser.resolve_google_maps_short_url",
        lambda _: "https://www.google.com/maps/place/OnlyAPlaceName/",
    )
    with pytest.raises(MalformedLocationInputError, match="stage=extract_coordinates_failed_after_resolution"):
        parse_google_maps_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")
