from __future__ import annotations

from dataclasses import dataclass
import json
import html
from typing import Literal
import ipaddress
import re
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import httpx
import structlog

MAX_LOCATION_INPUT_LEN = 2048
INPUT_KIND = Literal["decimal_pair", "degree_pair", "google_maps_url", "google_maps_short_url"]
_SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.page"}
logger = structlog.get_logger(__name__)
_BLOCKED_RESOLUTION_MESSAGE = (
    "This Google Maps short link could not be resolved automatically. Please paste the full Google Maps URL, "
    "coordinates."
)


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


class LocationNotSupportedError(LocationParseError):
    error_code = "LOCATION_NOT_SUPPORTED"


class ShortUrlResolutionError(LocationParseError):
    error_code = "SHORT_URL_RESOLUTION_FAILED"


class MalformedLocationInputError(LocationParseError):
    error_code = "MALFORMED_LOCATION_INPUT"


class LocationResolutionBlockedError(LocationParseError):
    error_code = "SHORT_URL_RESOLUTION_BLOCKED"


def _format_resolution_event_details(
    *,
    stage: str,
    short_url: str,
    current_url: str | None = None,
    response_url: str | None = None,
    final_url: str | None = None,
    status_code: int | str | None = None,
    redirect_hop: int | None = None,
    content_type: str | None = None,
) -> str:
    details: list[str] = [f"stage={stage}", f"short_url={short_url}"]
    if current_url:
        details.append(f"current_url={current_url}")
    if response_url:
        details.append(f"response_url={response_url}")
    if final_url:
        details.append(f"final_url={final_url}")
    if status_code is not None:
        details.append(f"status_code={status_code}")
    if redirect_hop is not None:
        details.append(f"redirect_hop={redirect_hop}")
    if content_type:
        details.append(f"content_type={content_type}")
    return "; ".join(details)


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


def _is_likely_google_block_page_coordinate_pair(lat: float, lng: float) -> bool:
    # Google serves default US datacenter locations when bot-blocked (e.g., Ashburn, VA or Seattle, WA)
    known_datacenters = [
        (39.026799, -77.844326), # Ashburn, VA
        (39.043076, -77.489766), # Ashburn, VA (Alternate)
        (47.618696, -121.899783), # Seattle, WA (approx)
    ]
    for center_lat, center_lng in known_datacenters:
        if abs(lat - center_lat) < 0.05 and abs(lng - center_lng) < 0.05:
            return True
    return False


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
    host = (host or "").strip().strip(".").lower()
    if host in _SHORT_HOSTS:
        return True
    return host == "google.com" or host.endswith(".google.com")


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
    try:
        lat, lng = float(match.group(1)), float(match.group(2))
        validate_lat_lng(lat, lng)
        return lat, lng
    except (ValueError, InvalidCoordinateRangeError) as exc:
        logger.info("extract_pair_failed", value=value[:200], error=str(exc))
        return None


def _is_private_or_local_host(host: str) -> bool:
    normalized = (host or "").strip().strip("[]").lower()
    if not normalized:
        return True
    if normalized == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_redirect_target(url: str, *, allow_short_hosts: bool) -> tuple[str, str]:
    # DNS rebinding risk is reduced here by refusing all non-Google domains at every hop.
    # We do not trust caller-provided hostnames; only Google-owned hosts are permitted.
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"}:
        raise ShortUrlResolutionError("Short link redirect used an unsupported URL scheme.")
    if _is_private_or_local_host(host):
        raise ShortUrlResolutionError(
            f"Short link redirect targeted a private or local host ('{host or 'unknown'}'), which is blocked."
        )
    if allow_short_hosts:
        if not _is_supported_google_host(host):
            raise ShortUrlResolutionError(
                f"Google Maps short link redirected to unsupported domain '{host or 'unknown'}'."
            )
    elif not (host == "google.com" or host.endswith(".google.com")):
        raise ShortUrlResolutionError(
            f"Google Maps short link did not resolve to a Google Maps domain ('{host or 'unknown'}')."
        )
    return host, parsed.path or "/"


def extract_lat_lng_from_google_maps_url(url: str) -> tuple[float, float, str]:
    decoded = unquote(url)

    query = parse_qs(urlparse(url).query)
    for key in ("q", "ll", "query", "center", "destination", "origin", "saddr", "daddr"):
        pair = _extract_pair(query.get(key, [None])[0])
        if pair:
            return pair[0], pair[1], f"query_{key}"

    place = re.search(r"!3d([+-]?\d+(?:\.\d+)?)!4d([+-]?\d+(?:\.\d+)?)", decoded)
    if place:
        lat, lng = float(place.group(1)), float(place.group(2))
        validate_lat_lng(lat, lng)
        return lat, lng, "place_3d4d"

    place_reverse = re.search(r"!2d([+-]?\d+(?:\.\d+)?)!3d([+-]?\d+(?:\.\d+)?)", decoded)
    if place_reverse:
        lng, lat = float(place_reverse.group(1)), float(place_reverse.group(2))
        validate_lat_lng(lat, lng)
        return lat, lng, "place_2d3d"

    viewport = re.search(r"@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", decoded)
    if viewport:
        lat, lng = float(viewport.group(1)), float(viewport.group(2))
        validate_lat_lng(lat, lng)
        return lat, lng, "viewport_center"

    raise MalformedLocationInputError(
        "Could not extract coordinates from the resolved Google Maps URL. "
        "The link may point to a place page without explicit coordinates."
    )


def _extract_lat_lng_from_google_maps_html(body: str | None) -> tuple[float, float, str] | None:
    if not body:
        return None

    decoded_body = unquote(body)
    patterns = (
        (r"!3d([+-]?\d+(?:\.\d+)?)!4d([+-]?\d+(?:\.\d+)?)", "html_place_3d4d"),
        (r"@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", "html_viewport_center"),
        (r'"lat"\s*:\s*([+-]?\d+(?:\.\d+)?)\s*,\s*"lng"\s*:\s*([+-]?\d+(?:\.\d+)?)', "html_lat_lng_json"),
        (
            r"(?:center|query|ll|destination|origin)=([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)",
            "html_query_pair",
        ),
        (
            r'"latitude"\s*:\s*([+-]?\d+(?:\.\d+)?)\s*,\s*"longitude"\s*:\s*([+-]?\d+(?:\.\d+)?)',
            "html_latitude_longitude_json",
        ),
    )
    for pattern, method in patterns:
        match = re.search(pattern, decoded_body)
        if not match:
            continue
        try:
            lat, lng = float(match.group(1)), float(match.group(2))
            validate_lat_lng(lat, lng)
            return lat, lng, method
        except (ValueError, InvalidCoordinateRangeError) as exc:
            logger.info("html_coord_extraction_failed", pattern=method, matched_groups=[match.group(1), match.group(2)], error=str(exc))
            continue
    logger.info("html_coord_extraction_no_match", body_length=len(decoded_body))
    return None


def _extract_html_redirect_url(body: str | None) -> str | None:
    if not body:
        return None
    try:
        # Meta refresh
        meta_match = re.search(r'http-equiv=["\']?refresh["\']?[^>]*content=["\']?\d+;\s*url\s*=\s*([^"\'>\s]+)', body, flags=re.IGNORECASE)
        # JS redirect
        js_match = re.search(r'(?:window\.)?location(?:\.href\s*=\s*|\.replace\(\s*)["\']([^"\']+)["\']', body, flags=re.IGNORECASE)

        extracted = (meta_match and meta_match.group(1)) or (js_match and js_match.group(1))
        if extracted:
            result = html.unescape(extracted.strip().strip("'\"" ))
            logger.info("html_redirect_url_extracted", source="meta_refresh" if meta_match else "js_location", extracted_url=result[:300])
            return result
        return None
    except Exception as exc:
        logger.info("html_redirect_extraction_error", error=str(exc), body_length=len(body))
        return None


def resolve_google_maps_short_url(raw: str, timeout_seconds: float = 4.0) -> str:
    normalized = _normalize_raw(raw)
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if host not in _SHORT_HOSTS:
        raise UnsupportedLocationInputError("This URL is not a supported Google Maps short link.")
    if not _is_supported_short_path(host, parsed.path):
        raise UnsupportedLocationInputError("This Google short link format is not supported.")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def _log_attempt(success: bool, failure_reason: str | None, http_status: int | None) -> None:
        logger.info(
            "location_resolve_attempt",
            input_type="google_short_url",
            resolver_strategy="redirect_follow",
            success=success,
            failure_reason=failure_reason,
            http_status=http_status,
            provider="google",
        )

    try:
        MAX_HTML_REDIRECTS = 3
        html_redirect_hops = 0
        current_url = normalized

        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
            while True:
                response = client.get(current_url)
                response.raise_for_status()

                for hop in response.history:
                    hop_url = str(hop.url)
                    hop_host, hop_path = _validate_redirect_target(hop_url, allow_short_hosts=True)
                    if hop_host in _SHORT_HOSTS and not _is_supported_short_path(hop_host, hop_path):
                        raise UnsupportedLocationInputError("This Google short link format is not supported.")

                final_url = str(response.url)
                final_host, _ = _validate_redirect_target(final_url, allow_short_hosts=False)
                
                body = getattr(response, "text", "")
                html_redirect_url = _extract_html_redirect_url(body)
                
                if html_redirect_url:
                    html_redirect_hops += 1
                    if html_redirect_hops > MAX_HTML_REDIRECTS:
                        _log_attempt(False, "max_html_redirects_exceeded", response.status_code)
                        raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE)
                    
                    current_url = urljoin(final_url, html_redirect_url)
                    _validate_redirect_target(current_url, allow_short_hosts=False)
                    continue

                body_lower = body.lower()
                blocked_markers = ("consent.google", "unusual traffic", "detected unusual", "/sorry/")
                if final_host == "consent.google.com" or "consent.google.com" in final_url or any(marker in body_lower for marker in blocked_markers):
                    _log_attempt(False, "bot_page_encountered", response.status_code)
                    raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE)

                try:
                    lat, lng, _ = extract_lat_lng_from_google_maps_url(final_url)
                    if _is_likely_google_block_page_coordinate_pair(lat, lng):
                        _log_attempt(False, "bot_page_datacenter_coords", response.status_code)
                        raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE)
                    _log_attempt(True, None, response.status_code)
                    return final_url
                except MalformedLocationInputError as exc:
                    html_pair = _extract_lat_lng_from_google_maps_html(body)
                    if html_pair:
                        lat, lng, _ = html_pair
                        if _is_likely_google_block_page_coordinate_pair(lat, lng):
                            _log_attempt(False, "bot_page_datacenter_coords", response.status_code)
                            raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE)
                        _log_attempt(True, None, response.status_code)
                        return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
                    _log_attempt(False, "coordinates_not_found", response.status_code)
                    raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE) from exc
    except httpx.TimeoutException as exc:
        _log_attempt(False, "timeout", None)
        raise ShortUrlResolutionError(
            "Google Maps short link resolution timed out. Please check your connection and try again."
        ) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        _log_attempt(False, "http_error", status)
        raise ShortUrlResolutionError(
            f"Google Maps short link returned HTTP {status} while resolving redirects."
        ) from exc
    except httpx.RequestError as exc:
        _log_attempt(False, "request_error", None)
        raise ShortUrlResolutionError(
            "Failed to resolve the Google Maps short link due to a network/protocol error."
        ) from exc
    except (LocationResolutionBlockedError, ShortUrlResolutionError, UnsupportedLocationInputError):
        raise
    except Exception as exc:
        _log_attempt(False, "unexpected_error", None)
        raise ShortUrlResolutionError(
            "Failed to resolve the Google Maps short link due to an unexpected resolver error."
        ) from exc


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
        try:
            lat, lng, method = extract_lat_lng_from_google_maps_url(resolved)
        except MalformedLocationInputError as exc:
            details = _format_resolution_event_details(
                stage="extract_coordinates_failed_after_resolution",
                short_url=normalized,
                final_url=resolved,
            )
            raise MalformedLocationInputError(
                "Could not extract coordinates from the resolved Google Maps short URL. "
                f"Event details: {details}."
            ) from exc
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


def _extract_lat_lng_from_google_maps_html(body: str | None) -> tuple[float, float, str] | None:
    if not body:
        return None

    decoded_body = unquote(body)
    patterns = (
        (r"!3d([+-]?\d+(?:\.\d+)?)!4d([+-]?\d+(?:\.\d+)?)", "html_place_3d4d"),
        (r"@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", "html_viewport_center"),
        (r'"lat"\s*:\s*([+-]?\d+(?:\.\d+)?)\s*,\s*"lng"\s*:\s*([+-]?\d+(?:\.\d+)?)', "html_lat_lng_json"),
        (
            r"(?:center|query|ll|destination|origin)=([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)",
            "html_query_pair",
        ),
        (
            r'"latitude"\s*:\s*([+-]?\d+(?:\.\d+)?)\s*,\s*"longitude"\s*:\s*([+-]?\d+(?:\.\d+)?)',
            "html_latitude_longitude_json",
        ),
    )
    for pattern, method in patterns:
        match = re.search(pattern, decoded_body)
        if not match:
            continue
        lat, lng = float(match.group(1)), float(match.group(2))
        validate_lat_lng(lat, lng)
        return lat, lng, method
    return None


def _extract_html_redirect_url(body: str | None) -> str | None:
    if not body:
        return None
    # Meta refresh
    meta_match = re.search(r'http-equiv=["\']?refresh["\']?[^>]*content=["\']?\d+;\s*url\s*=\s*([^"\'>\s]+)', body, flags=re.IGNORECASE)
    # JS redirect
    js_match = re.search(r'(?:window\.)?location(?:\.href\s*=\s*|\.replace\(\s*)["\']([^"\']+)["\']', body, flags=re.IGNORECASE)
    
    extracted = (meta_match and meta_match.group(1)) or (js_match and js_match.group(1))
    if extracted:
        return html.unescape(extracted.strip().strip("'\""))
    return None


def resolve_google_maps_short_url(raw: str, timeout_seconds: float = 4.0) -> str:
    normalized = _normalize_raw(raw)
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if host not in _SHORT_HOSTS:
        raise UnsupportedLocationInputError("This URL is not a supported Google Maps short link.")
    if not _is_supported_short_path(host, parsed.path):
        raise UnsupportedLocationInputError("This Google short link format is not supported.")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    def _log_attempt(success: bool, failure_reason: str | None, http_status: int | None) -> None:
        logger.info(
            json.dumps(
                {
                    "event": "location_resolve_attempt",
                    "input_type": "google_short_url",
                    "resolver_strategy": "redirect_follow",
                    "success": success,
                    "failure_reason": failure_reason,
                    "http_status": http_status,
                    "provider": "google",
                }
            )
        )

    try:
        MAX_HTML_REDIRECTS = 3
        html_redirect_hops = 0
        current_url = normalized

        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
            while True:
                response = client.get(current_url)
                response.raise_for_status()

                for hop in response.history:
                    hop_url = str(hop.url)
                    hop_host, hop_path = _validate_redirect_target(hop_url, allow_short_hosts=True)
                    if hop_host in _SHORT_HOSTS and not _is_supported_short_path(hop_host, hop_path):
                        raise UnsupportedLocationInputError("This Google short link format is not supported.")

                final_url = str(response.url)
                final_host, _ = _validate_redirect_target(final_url, allow_short_hosts=False)
                
                body = getattr(response, "text", "")
                html_redirect_url = _extract_html_redirect_url(body)
                
                if html_redirect_url:
                    html_redirect_hops += 1
                    if html_redirect_hops > MAX_HTML_REDIRECTS:
                        _log_attempt(False, "max_html_redirects_exceeded", response.status_code)
                        raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE)
                    
                    current_url = urljoin(final_url, html_redirect_url)
                    _validate_redirect_target(current_url, allow_short_hosts=False)
                    continue

                body_lower = body.lower()
                blocked_markers = ("consent.google", "unusual traffic", "detected unusual", "/sorry/")
                if final_host == "consent.google.com" or "consent.google.com" in final_url or any(marker in body_lower for marker in blocked_markers):
                    _log_attempt(False, "bot_page_encountered", response.status_code)
                    raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE)

                try:
                    lat, lng, _ = extract_lat_lng_from_google_maps_url(final_url)
                    if _is_likely_google_block_page_coordinate_pair(lat, lng):
                        _log_attempt(False, "bot_page_datacenter_coords", response.status_code)
                        raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE)
                    _log_attempt(True, None, response.status_code)
                    return final_url
                except MalformedLocationInputError as exc:
                    html_pair = _extract_lat_lng_from_google_maps_html(body)
                    if html_pair:
                        lat, lng, _ = html_pair
                        if _is_likely_google_block_page_coordinate_pair(lat, lng):
                            _log_attempt(False, "bot_page_datacenter_coords", response.status_code)
                            raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE)
                        _log_attempt(True, None, response.status_code)
                        return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
                    _log_attempt(False, "coordinates_not_found", response.status_code)
                    raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE) from exc
    except httpx.TimeoutException as exc:
        _log_attempt(False, "timeout", None)
        raise ShortUrlResolutionError(
            "Google Maps short link resolution timed out. Please check your connection and try again."
        ) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        _log_attempt(False, "http_error", status)
        raise ShortUrlResolutionError(
            f"Google Maps short link returned HTTP {status} while resolving redirects."
        ) from exc
    except httpx.RequestError as exc:
        _log_attempt(False, "request_error", None)
        raise ShortUrlResolutionError(
            "Failed to resolve the Google Maps short link due to a network/protocol error."
        ) from exc
    except (LocationResolutionBlockedError, ShortUrlResolutionError, UnsupportedLocationInputError):
        raise
    except Exception as exc:
        _log_attempt(False, "unexpected_error", None)
        raise ShortUrlResolutionError(
            "Failed to resolve the Google Maps short link due to an unexpected resolver error."
        ) from exc


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
        try:
            lat, lng, method = extract_lat_lng_from_google_maps_url(resolved)
        except MalformedLocationInputError as exc:
            details = _format_resolution_event_details(
                stage="extract_coordinates_failed_after_resolution",
                short_url=normalized,
                final_url=resolved,
            )
            raise MalformedLocationInputError(
                "Could not extract coordinates from the resolved Google Maps short URL. "
                f"Event details: {details}."
            ) from exc
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
        parsed = parse_decimal_pair(normalized)
    elif "°" in normalized and re.search(r"[NSEW]", normalized, re.IGNORECASE):
        parsed = parse_degree_pair(normalized)
    elif re.match(r"^https?://", normalized, re.IGNORECASE):
        parsed = parse_google_maps_url(normalized)
    else:
        raise UnsupportedLocationInputError("Unsupported location input format.")

    if not (15.9 <= parsed.latitude <= 16.3 and 107.8 <= parsed.longitude <= 108.4):
        logger.info(
            "location_outside_supported_region",
            latitude=parsed.latitude,
            longitude=parsed.longitude,
            supported_region="Da Nang, Vietnam",
        )
        raise LocationNotSupportedError("This location is not supported currently")

    return parsed
