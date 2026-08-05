import asyncio
from dataclasses import asdict

import pytest

import standalone_gps_resolver as standalone
from app.services import location_parser as application


@pytest.mark.parametrize(
    "raw",
    [
        "16.0544, 108.2022",
        "16.0544 108.2022",
        "108.2022,16.0544",
        "16°03'15.8\"N 108°12'07.9\"E",
        "https://www.google.com/maps/search/?api=1&q=16.0544%2C108.2022",
        "https://maps.google.com/maps?ll=16.0544%2C108.2022",
        "https://www.google.com/maps/search/?query=16.0544%2C108.2022",
        "https://www.google.com/maps?center=16.0544%2C108.2022",
        "https://www.google.com/maps/dir/?destination=16.0544%2C108.2022",
        "https://www.google.com/maps/dir/?origin=16.0544%2C108.2022",
        "https://www.google.com/maps/dir/?saddr=16.0544%2C108.2022",
        "https://www.google.com/maps/dir/?daddr=16.0544%2C108.2022",
        "https://www.google.com/maps/place/x/@16.1,108.1,17z/data=!3d16.0544!4d108.2022",
        "https://www.google.com/maps/place/x/data=!2d108.2022!3d16.0544",
        "https://www.google.com/maps/place/x/@16.0544,108.2022,17z",
        "https://www.google.com/maps/place/16%C2%B003'15.8%22N+108%C2%B012'07.9%22E",
    ],
)
def test_standalone_parser_matches_application_parser(raw):
    assert asdict(standalone.parse_location_input(raw)) == asdict(application.parse_location_input(raw))


@pytest.mark.parametrize(
    "short_url",
    [
        "https://maps.app.goo.gl/example",
        "https://goo.gl/maps/example",
        "https://g.page/example",
    ],
)
def test_standalone_short_link_hosts_use_existing_redirect_resolution(short_url):
    final_url = "https://www.google.com/maps/place/x/@16.0544,108.2022,17z"

    class _Response:
        url = final_url
        status_code = 200
        history = []

        @staticmethod
        def raise_for_status():
            return None

    class _Client:
        async def get(self, *_args, **_kwargs):
            return _Response()

    parsed = asyncio.run(standalone.parse_location_input_async(short_url, http_client=_Client()))

    assert parsed.input_kind == "google_maps_short_url"
    assert parsed.latitude == pytest.approx(16.0544)
    assert parsed.longitude == pytest.approx(108.2022)
    assert parsed.source_url == final_url
    assert parsed.resolution_method == "viewport_center"


@pytest.mark.parametrize(
    "future_input",
    [
        "Tashi Ocean Garden Hotel Da Nang",
        "21 Phước Trường 11, Đà Nẵng, Vietnam",
        "ChIJi8-oPZEXQjER7SrBBvMXhzE",
        "0x3142110057160fd7:0x3ee5e169266ed6c2",
    ],
)
def test_future_api_paths_remain_comment_only(future_input):
    with pytest.raises(standalone.UnsupportedLocationInputError):
        standalone.parse_location_input(future_input)
