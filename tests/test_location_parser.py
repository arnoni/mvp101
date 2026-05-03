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
    LocationResolutionBlockedError,
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


def test_parse_google_long_place_3d_4d_with_g_ep():
    url = "https://www.google.com/maps/place/x/data=!8m2!3d16.0199548!4d108.2548651!16s%2Fg%2F11ysbcwnvh!18m1!1e1?entry=ttu&g_ep=EgoyMDI2MDQyMS4wIKXMDSoASAFQAw%3D%3D"
    parsed = parse_google_maps_url(url)
    assert parsed.resolution_method == "place_3d4d"
    assert parsed.latitude == pytest.approx(16.0199548)
    assert parsed.longitude == pytest.approx(108.2548651)




def test_parse_google_long_url_with_encoded_dms_and_place_coordinates():
    url = "https://www.google.com/maps/place/16%C2%B004'28.0%22N+108%C2%B014'38.5%22E/@16.0744512,108.2414626,17z/data=!3m1!4b1!4m4!3m3!8m2!3d16.0744512!4d108.2440375"
    parsed = parse_google_maps_url(url)
    assert parsed.resolution_method == "place_3d4d"
    assert parsed.latitude == pytest.approx(16.0744512)
    assert parsed.longitude == pytest.approx(108.2440375)


def test_parse_google_long_url_with_encoded_dms_only():
    url = "https://www.google.com/maps/place/16%C2%B004'28.0%22N+108%C2%B014'38.5%22E"
    parsed = parse_google_maps_url(url)
    assert parsed.resolution_method == "decoded_dms"
    assert parsed.latitude == pytest.approx(16.074444, rel=1e-6)
    assert parsed.longitude == pytest.approx(108.244028, rel=1e-6)


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
        history = []

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
    with pytest.raises(ShortUrlResolutionError, match="HTTP 503"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")


def test_short_url_resolution_returns_parseable_final_url(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.url = "https://www.google.com/maps/place/x/@16.0544,108.2022,17z"
    response.text = "<html/>"
    response.history = []
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
    assert resolved == "https://www.google.com/maps/place/x/@16.0544,108.2022,17z"


def test_short_url_resolution_raises_blocked_error_when_coords_not_in_final_url(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.url = "https://www.google.com/maps/place/KIM+Sui+cao/"
    response.text = "<html>normal place page without coordinates</html>"
    response.history = []
    response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    with pytest.raises(LocationResolutionBlockedError, match="could not be resolved automatically"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")


def test_short_url_resolution_blocks_consent_page(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.url = "https://consent.google.com/m?continue=https://www.google.com/maps"
    response.text = "<html>consent page</html>"
    response.history = []
    response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    with pytest.raises(LocationResolutionBlockedError, match="could not be resolved automatically"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")


def test_short_url_resolution_logs_structured_attempt_payload(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.url = "https://consent.google.com/m?continue=https://www.google.com/maps"
    response.text = "<html>consent page</html>"
    response.history = []
    response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return response

    log_spy = Mock()
    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    monkeypatch.setattr("app.services.location_parser.logger.info", log_spy)

    with pytest.raises(LocationResolutionBlockedError):
        resolve_google_maps_short_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")

    logged_payload = log_spy.call_args.args[0]
    assert '"event": "location_resolve_attempt"' in logged_payload
    assert '"success": false' in logged_payload
    assert '"failure_reason": "bot_page_encountered"' in logged_payload
    assert '"http_status": 200' in logged_payload


def test_short_url_resolution_uses_native_http_redirect_following(monkeypatch):
    captured_kwargs = {}

    class StubResponse:
        status_code = 200
        url = "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022"
        text = ""
        history = []

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
    assert captured_kwargs["headers"] == {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    assert resolved == "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022"


@pytest.mark.parametrize("blocked_url", ["http://127.0.0.1/internal", "http://169.254.10.20/latest", "http://10.0.0.5/admin"])
def test_short_url_resolution_redirect_chain_blocks_private_hosts(monkeypatch, blocked_url):
    first_response = Mock(url=blocked_url)
    final_response = Mock()
    final_response.status_code = 200
    final_response.url = "https://www.google.com/maps/search/?api=1&query=16.0544,108.2022"
    final_response.text = ""
    final_response.history = [first_response]
    final_response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return final_response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    with pytest.raises(ShortUrlResolutionError, match="private or local host"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")


def test_short_url_resolution_redirect_chain_blocks_non_google_final_host(monkeypatch):
    response = Mock()
    response.status_code = 200
    response.url = "https://example.com/maps/place/x/@16.0544,108.2022,17z"
    response.text = ""
    response.history = []
    response.raise_for_status.return_value = None

    class StubClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, _):
            return response

    monkeypatch.setattr("app.services.location_parser.httpx.Client", lambda **_: StubClient())
    with pytest.raises(ShortUrlResolutionError, match="did not resolve to a Google Maps domain"):
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
