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
    _build_short_url_cache_key_input,
    LocationResolutionBlockedError,
    MalformedLocationInputError,
    ShortUrlResolutionError,
    UnsupportedLocationInputError,
    parse_decimal_pair,
    parse_degree_pair,
    parse_google_maps_url,
    parse_location_input,
    resolve_google_maps_short_url,
    parse_google_maps_url_async,
    parse_location_input_async,
)


TASHI_BROWSER_URL = "https://www.google.com/maps/place/Tashi+Ocean+Garden+Hotel+%26+Apartment+Da+Nang/@16.077462,108.2419443,19z/data=!4m20!1m8!3m7!1s0x314217893f8ae817:0xb2eb103f179a78ed!2zMjEgUGjGsOG7m2MgVHLGsOG7nW5nIDExLCBBbiBI4bqjaSwgxJDDoCBO4bq1bmcgNTUwMDAwLCBWaWV0bmFt!3b1!8m2!3d16.0777299!4d108.242299!16s%2Fg%2F11jyly4fyb!3m10!1s0x31421714368a092b:0x318717f306c12aec!5m4!1s2026-05-23!2i3!4m1!1i2!8m2!3d16.0776236!4d108.2426482!16s%2Fg%2F11kbp5srhq!18m1!1e1?entry=ttu&g_ep=EgoyMDI2MDUxMy4wIKXMDSoASAFQAw%3D%3D"


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


def test_sync_short_url_resolution_intentionally_unsupported():
    with pytest.raises(ShortUrlResolutionError, match="use async parser flow"):
        resolve_google_maps_short_url("https://maps.app.goo.gl/MXmuC4XEuLnUY5rR8")


def test_sync_parse_location_input_short_url_fails_with_clear_error():
    with pytest.raises(UnsupportedLocationInputError, match="async parser flow"):
        parse_location_input("https://maps.app.goo.gl/gTP5u9ELrzyaCU37A")


def test_parse_google_invalid_goo_gl_short_path_rejected():
    with pytest.raises(UnsupportedLocationInputError):
        parse_google_maps_url("https://goo.gl/not-maps")


def test_parse_google_short_url_sync_requires_async():
    with pytest.raises(UnsupportedLocationInputError, match="async parser flow"):
        parse_google_maps_url("https://maps.app.goo.gl/gPZ5VtapJLqfSBnZ9?g_st=aw")


def test_regression_browser_format_url_coordinates_unchanged():
    parsed = parse_google_maps_url(TASHI_BROWSER_URL)
    assert parsed.latitude == pytest.approx(16.0777299)
    assert parsed.longitude == pytest.approx(108.242299)


def test_sync_parse_location_input_browser_format_url():
    parsed = parse_location_input(TASHI_BROWSER_URL)
    assert parsed.latitude == pytest.approx(16.0777299)
    assert parsed.longitude == pytest.approx(108.242299)


@pytest.mark.asyncio
async def test_async_parse_location_input_browser_format_url():
    parsed = await parse_location_input_async(TASHI_BROWSER_URL)
    assert parsed.latitude == pytest.approx(16.0777299)
    assert parsed.longitude == pytest.approx(108.242299)


@pytest.mark.asyncio
async def test_short_url_handoff_to_existing_google_parser(monkeypatch):
    final_url = "https://www.google.com/maps/place/Tashi+Ocean+Garden+Hotel+%26+Apartment+Da+Nang/@16.077462,108.2419443,19z/data=!4m11!3m10!1s0x31421714368a092b:0x318717f306c12aec!5m4!1s2026-05-23!2i3!4m1!1i2!8m2!3d16.0776236!4d108.2426482!16s%2Fg%2F11kbp5srhq!18m1!1e1?entry=tts&g_ep=EgoyMDI2MDUxMy4wIPu8ASoASAFQAw%3D%3D"
    monkeypatch.setattr("app.services.location_parser.resolve_google_maps_short_url_async", Mock(return_value=final_url))
    parsed = await parse_location_input_async("https://maps.app.goo.gl/gTP5u9ELrzyaCU37A")
    expected = parse_google_maps_url(final_url)
    assert parsed.latitude == pytest.approx(expected.latitude)
    assert parsed.longitude == pytest.approx(expected.longitude)


@pytest.mark.asyncio
async def test_short_url_and_resolved_url_equivalence(monkeypatch):
    final_url = TASHI_BROWSER_URL
    monkeypatch.setattr("app.services.location_parser.resolve_google_maps_short_url_async", Mock(return_value=final_url))
    direct = await parse_google_maps_url_async(final_url)
    short = await parse_google_maps_url_async("https://maps.app.goo.gl/gTP5u9ELrzyaCU37A")
    assert short.latitude == pytest.approx(direct.latitude)
    assert short.longitude == pytest.approx(direct.longitude)


def test_short_url_cache_key_strips_tracking_params_only():
    with_tracking = _build_short_url_cache_key_input("https://maps.app.goo.gl/T6vt1WLaSBz9JoTh6?g_st=aw")
    without_tracking = _build_short_url_cache_key_input("https://maps.app.goo.gl/T6vt1WLaSBz9JoTh6")
    with_unknown = _build_short_url_cache_key_input("https://maps.app.goo.gl/T6vt1WLaSBz9JoTh6?foo=bar")
    full_url = _build_short_url_cache_key_input("https://www.google.com/maps/place/x?g_st=aw")
    assert with_tracking == without_tracking
    assert with_unknown != without_tracking
    assert full_url.endswith("?g_st=aw")


@pytest.mark.asyncio
async def test_short_url_resolution_structured_log_success(monkeypatch):
    final_url = "https://www.google.com/maps/place/X/@16.0101,108.2202,17z"
    logs = []

    class _Logger:
        def info(self, _msg, **kwargs):
            logs.append(("info", kwargs))
        def warning(self, _msg, **kwargs):
            logs.append(("warning", kwargs))

    monkeypatch.setattr("app.services.location_parser.resolve_google_maps_short_url_async", Mock(return_value=final_url))
    monkeypatch.setattr("app.services.location_parser.logger", _Logger())
    parsed = await parse_google_maps_url_async("https://maps.app.goo.gl/ABC123?g_st=aw")
    assert parsed.latitude == pytest.approx(16.0101)
    assert parsed.longitude == pytest.approx(108.2202)
    assert any(
        lvl == "info"
        and rec.get("event") == "google_maps_short_url_resolution_attempted"
        and rec.get("resolution_result") == "success"
        and rec.get("parsed_lat") == pytest.approx(16.0101)
        and rec.get("parsed_lon") == pytest.approx(108.2202)
        for lvl, rec in logs
    )


@pytest.mark.asyncio
async def test_short_url_resolution_structured_log_blocked(monkeypatch):
    logs = []

    class _Logger:
        def info(self, _msg, **kwargs):
            logs.append(("info", kwargs))
        def warning(self, _msg, **kwargs):
            logs.append(("warning", kwargs))

    async def _blocked(*_args, **_kwargs):
        raise LocationResolutionBlockedError("blocked")

    monkeypatch.setattr("app.services.location_parser.resolve_google_maps_short_url_async", _blocked)
    monkeypatch.setattr("app.services.location_parser.logger", _Logger())
    with pytest.raises(LocationResolutionBlockedError):
        await parse_google_maps_url_async("https://maps.app.goo.gl/ABC123")
    assert any(
        lvl == "warning"
        and rec.get("event") == "google_maps_short_url_resolution_attempted"
        and rec.get("resolution_result") == "blocked"
        and rec.get("error_code") == "SHORT_URL_RESOLUTION_BLOCKED"
        for lvl, rec in logs
    )


@pytest.mark.asyncio
async def test_short_url_non_parseable_path_logs_without_structlog_event_conflict(monkeypatch):
    logs = []

    class _Logger:
        def info(self, _msg, **kwargs):
            logs.append(("info", kwargs))
        def warning(self, _msg, **kwargs):
            logs.append(("warning", kwargs))

    final_url = "https://www.google.com/maps/place/NoCoords/data=!4m2!3m1!1s0x3142110057160fd7:0x3ee5e169266ed6c2"
    monkeypatch.setattr("app.services.location_parser.resolve_google_maps_short_url_async", Mock(return_value=final_url))
    monkeypatch.setattr("app.services.location_parser.logger", _Logger())
    with pytest.raises(MalformedLocationInputError):
        await parse_google_maps_url_async("https://maps.app.goo.gl/ABC123")
    assert any(rec.get("resolution_result") in {"non_parseable", "place_page_without_coordinates"} for _, rec in logs)


@pytest.mark.asyncio
async def test_short_url_place_page_without_coordinates(monkeypatch):
    logs = []

    class _Logger:
        def info(self, _msg, **kwargs):
            logs.append(("info", kwargs))
        def warning(self, _msg, **kwargs):
            logs.append(("warning", kwargs))

    final_url = (
        "https://www.google.com/maps/place/Tiny+Food+%26+Drink/data="
        "!4m2!3m1!1s0x3142110057160fd7:0x3ee5e169266ed6c2!18m1!1e1"
        "?utm_source=mstt_1&entry=gps&g_st=aw"
    )
    monkeypatch.setattr("app.services.location_parser.resolve_google_maps_short_url_async", Mock(return_value=final_url))
    monkeypatch.setattr("app.services.location_parser.logger", _Logger())
    with pytest.raises(MalformedLocationInputError, match="does not expose coordinates"):
        await parse_google_maps_url_async("https://maps.app.goo.gl/T6vt1WLaSBz9JoTh6?g_st=aw")
    assert any(
        rec.get("resolution_result") == "place_page_without_coordinates"
        and rec.get("error_code") == "RESOLVED_PLACE_PAGE_NO_COORDS"
        and rec.get("resolved_url_format") == "place_id_only"
        for _, rec in logs
    )


@pytest.mark.asyncio
async def test_tracking_param_stripped_before_resolved_url_parsing(monkeypatch):
    final_url_with = "https://www.google.com/maps/place/x/@16.0101,108.2202,17z?g_st=aw"
    final_url_without = "https://www.google.com/maps/place/x/@16.0101,108.2202,17z"
    monkeypatch.setattr("app.services.location_parser.resolve_google_maps_short_url_async", Mock(return_value=final_url_with))
    a = await parse_google_maps_url_async("https://maps.app.goo.gl/with")
    monkeypatch.setattr("app.services.location_parser.resolve_google_maps_short_url_async", Mock(return_value=final_url_without))
    b = await parse_google_maps_url_async("https://maps.app.goo.gl/without")
    assert a.latitude == pytest.approx(b.latitude)
    assert a.longitude == pytest.approx(b.longitude)


@pytest.mark.asyncio
async def test_resolver_bypasses_coordinate_free_cached_value_and_refetches():
    from app.services.location_parser import _build_short_url_cache_key_input, resolve_google_maps_short_url_async
    raw = "https://maps.app.goo.gl/T6vt1WLaSBz9JoTh6?g_st=aw"
    cache_key_input = _build_short_url_cache_key_input(raw)
    import hashlib
    key = f"maps:expand:{hashlib.sha256(cache_key_input.encode('utf-8')).hexdigest()[:16]}"

    class _Redis:
        def __init__(self):
            self.calls = 0
            self.store = {
                key: "https://www.google.com/maps/place/NoCoords/data=!4m2!3m1!1s0x3142110057160fd7:0x3ee5e169266ed6c2"
            }
        async def get(self, key):
            self.calls += 1
            return self.store.get(key)
        async def setex(self, key, ttl, value):
            self.store[key] = value

    class _Resp:
        def __init__(self, url):
            self.url = url
            self.status_code = 200
            self.history = []
        def raise_for_status(self):
            return None

    class _Client:
        async def get(self, *_args, **_kwargs):
            return _Resp("https://www.google.com/maps/place/x/@16.0101,108.2202,17z")

    redis = _Redis()
    resolved = await resolve_google_maps_short_url_async(
        raw,
        redis_client=redis,
        http_client=_Client(),
    )
    assert "@16.0101,108.2202" in resolved
