from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import re
from urllib.parse import urlparse, parse_qs, unquote

import httpx

MAX_LOCATION_INPUT_LEN = 2048
INPUT_KIND = Literal["decimal_pair", "degree_pair", "google_maps_url", "google_maps_short_url"]
_GOOGLE_HOST_SUFFIXES = ("google.com", "maps.google.com")
_SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.page"}


@dataclass(frozen=True)
class ParsedLocationInput:
    input_kind: INPUT_KIND
    original_input: str
    normalized_input: str
    latitude: float
    longitude: float
    source_url: str | None = None
    resolution_method: str | None = None


class LocationParseError(ValueError):
    error_code = "INVALID_LOCATION_INPUT"


class UnsupportedLocationInputError(LocationParseError):
    error_code = "UNSUPPORTED_LOCATION_INPUT"


class InvalidCoordinateRangeError(LocationParseError):
    error_code = "INVALID_COORDINATE_RANGE"


class ShortUrlResolutionError(LocationParseError):
    error_code = "SHORT_URL_RESOLUTION_FAILED"


class MalformedLocationInputError(LocationParseError):
    error_code = "MALFORMED_LOCATION_INPUT"


def _normalize_raw(raw: str) -> str:
    normalized = (raw or "").replace("\u00A0", " ").strip()
    normalized = normalized.replace("\u2018", "'").replace("\u2019", "'")
    normalized = normalized.replace("\u201C", '"').replace("\u201D", '"')
    return normalized


def _validate_raw(raw: str) -> None:
    if not raw:
        raise UnsupportedLocationInputError("Location input is required.")
    if len(raw) > MAX_LOCATION_INPUT_LEN:
        raise MalformedLocationInputError("Location input exceeds maximum length.")
    if any(ord(ch) < 32 for ch in raw):
        raise MalformedLocationInputError("Location input contains unsupported control characters.")


def validate_lat_lng(lat: float, lng: float) -> tuple[float, float]:
    if lat < -90 or lat > 90:
        raise InvalidCoordinateRangeError("Latitude must be between -90 and 90.")
    if lng < -180 or lng > 180:
        raise InvalidCoordinateRangeError("Longitude must be between -180 and 180.")
    return lat, lng


def parse_decimal_pair(raw: str) -> ParsedLocationInput:
    normalized = _normalize_raw(raw)
    if re.fullmatch(r"\d+,\d+\s+\d+,\d+", normalized):
        raise MalformedLocationInputError("Locale decimal commas are not supported. Use decimal point.")

    if "," in normalized:
        parts = [p.strip() for p in normalized.split(",")]
    else:
        parts = [p.strip() for p in normalized.split()]

    if len(parts) != 2:
        raise MalformedLocationInputError("Enter exactly two coordinate values.")

    try:
        first = float(parts[0])
        second = float(parts[1])
    except ValueError as exc:
        raise MalformedLocationInputError("Invalid decimal coordinates.") from exc

    lat, lng = first, second
    method = "decimal_pair"
    if abs(first) > 90 and abs(first) <= 180 and abs(second) <= 90:
        lat, lng = second, first
        method = "decimal_pair_reversed"

    validate_lat_lng(lat, lng)
    return ParsedLocationInput(
        input_kind="decimal_pair",
        original_input=raw,
        normalized_input=f"{lat:.6f}, {lng:.6f}",
        latitude=lat,
        longitude=lng,
        resolution_method=method,
    )


def _parse_dms_component(part: str) -> tuple[float, str]:
    match = re.search(r"(\d{1,3})\D+(\d{1,2})\D+(\d{1,2}(?:\.\d+)?)\D*([NSEW])", part, re.IGNORECASE)
    if not match:
        raise MalformedLocationInputError("Invalid degree coordinate format.")

    degrees = float(match.group(1))
    minutes = float(match.group(2))
    seconds = float(match.group(3))
    hemi = match.group(4).upper()

    if minutes >= 60 or seconds >= 60:
        raise MalformedLocationInputError("Degree minutes/seconds are out of range.")

    value = degrees + minutes / 60 + seconds / 3600
    if hemi in {"S", "W"}:
        value *= -1
    return value, hemi


def parse_degree_pair(raw: str) -> ParsedLocationInput:
    normalized = _normalize_raw(raw)
    parts = re.findall(r"\d{1,3}[^NSEW]*[NSEW]", normalized, re.IGNORECASE)
    if len(parts) != 2:
        raise MalformedLocationInputError("Degree format must include one latitude and one longitude value.")

    first_value, first_hemi = _parse_dms_component(parts[0])
    second_value, second_hemi = _parse_dms_component(parts[1])

    lat = first_value if first_hemi in {"N", "S"} else second_value
    lng = first_value if first_hemi in {"E", "W"} else second_value

    if first_hemi not in {"N", "S"} and second_hemi not in {"N", "S"}:
        raise MalformedLocationInputError("Missing latitude hemisphere marker.")
    if first_hemi not in {"E", "W"} and second_hemi not in {"E", "W"}:
        raise MalformedLocationInputError("Missing longitude hemisphere marker.")

    validate_lat_lng(lat, lng)
    return ParsedLocationInput(
        input_kind="degree_pair",
        original_input=raw,
        normalized_input=f"{lat:.6f}, {lng:.6f}",
        latitude=lat,
        longitude=lng,
        resolution_method="degree_pair",
    )


def _is_supported_google_host(host: str) -> bool:
    host = host.lower()
    if host in _SHORT_HOSTS:
        return True
    return host == "google.com" or host.endswith(".google.com") or host in _GOOGLE_HOST_SUFFIXES


def _is_supported_short_path(host: str, path: str) -> bool:
    normalized_path = path or "/"
    if host == "goo.gl":
        return normalized_path == "/maps" or normalized_path.startswith("/maps/")
    if host == "g.page":
        return normalized_path != "/"
    return True


def _extract_pair(value: str | None) -> tuple[float, float] | None:
    if not value:
        return None
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)", value)
    if not match:
        return None
    lat, lng = float(match.group(1)), float(match.group(2))
    validate_lat_lng(lat, lng)
    return lat, lng


def extract_lat_lng_from_google_maps_url(url: str) -> tuple[float, float, str]:
    decoded = unquote(url)

    place = re.search(r"!3d([+-]?\d+(?:\.\d+)?)!4d([+-]?\d+(?:\.\d+)?)", decoded)
    if place:
        lat, lng = float(place.group(1)), float(place.group(2))
        validate_lat_lng(lat, lng)
        return lat, lng, "place_3d4d"

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("q", "ll"):
        pair = _extract_pair(query.get(key, [None])[0])
        if pair:
            return pair[0], pair[1], f"query_{key}"

    viewport = re.search(r"@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", decoded)
    if viewport:
        lat, lng = float(viewport.group(1)), float(viewport.group(2))
        validate_lat_lng(lat, lng)
        return lat, lng, "viewport_center"

    raise MalformedLocationInputError("Could not extract coordinates from this Google Maps link.")


def resolve_google_maps_short_url(raw: str, timeout_seconds: float = 4.0) -> str:
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, max_redirects=5) as client:
            response = client.get(raw)
    except httpx.TimeoutException as exc:
        raise ShortUrlResolutionError("Short URL resolution timed out. Please try again.") from exc
    except httpx.HTTPError as exc:
        raise ShortUrlResolutionError("Could not resolve short URL. Please try again.") from exc

    final_url = str(response.url)
    final_host = (response.url.host or "").lower()
    if not _is_supported_google_host(final_host):
        raise ShortUrlResolutionError("Short URL resolved to an unsupported domain.")
    return final_url


def parse_google_maps_url(raw: str) -> ParsedLocationInput:
    normalized = _normalize_raw(raw)
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if not _is_supported_google_host(host):
        raise UnsupportedLocationInputError("Only Google Maps URLs are supported.")

    if host in _SHORT_HOSTS:
        if not _is_supported_short_path(host, parsed.path):
            raise UnsupportedLocationInputError("Only Google Maps short URLs are supported.")
        resolved = resolve_google_maps_short_url(normalized)
        lat, lng, method = extract_lat_lng_from_google_maps_url(resolved)
        return ParsedLocationInput(
            input_kind="google_maps_short_url",
            original_input=raw,
            normalized_input=f"{lat:.6f}, {lng:.6f}",
            latitude=lat,
            longitude=lng,
            source_url=resolved,
            resolution_method=method,
        )

    lat, lng, method = extract_lat_lng_from_google_maps_url(normalized)
    return ParsedLocationInput(
        input_kind="google_maps_url",
        original_input=raw,
        normalized_input=f"{lat:.6f}, {lng:.6f}",
        latitude=lat,
        longitude=lng,
        source_url=normalized,
        resolution_method=method,
    )


def parse_location_input(raw: str) -> ParsedLocationInput:
    normalized = _normalize_raw(raw)
    _validate_raw(normalized)

    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?", normalized) or re.fullmatch(
        r"[+-]?\d+(?:\.\d+)?\s+[+-]?\d+(?:\.\d+)?", normalized
    ):
        return parse_decimal_pair(normalized)

    if "°" in normalized and re.search(r"[NSEW]", normalized, re.IGNORECASE):
        return parse_degree_pair(normalized)

    if re.match(r"^https?://", normalized, re.IGNORECASE):
        return parse_google_maps_url(normalized)

    raise UnsupportedLocationInputError("Unsupported location input format.")
