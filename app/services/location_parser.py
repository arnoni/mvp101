from __future__ import annotations

from dataclasses import dataclass
import html
from typing import Literal
import ipaddress
import re
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import httpx

MAX_LOCATION_INPUT_LEN = 2048
INPUT_KIND = Literal["decimal_pair", "degree_pair", "google_maps_url", "google_maps_short_url"]
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


def _is_likely_google_block_page_coordinate_pair(lat: float, lng: float) -> bool:
    # Observed fallback coordinates from Google consent/bot-protection pages
    # frequently resolve to Ashburn, VA around (39.026799, -77.844326).
    return abs(lat - 39.026799) <= 0.01 and abs(lng - (-77.844326)) <= 0.01


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
    lat, lng = float(match.group(1)), float(match.group(2))
    validate_lat_lng(lat, lng)
    return lat, lng


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
        lat, lng = float(match.group(1)), float(match.group(2))
        validate_lat_lng(lat, lng)
        return lat, lng, method
    return None


def _extract_html_redirect_url(body: str | None) -> str | None:
    if not body:
        return None

    meta_refresh = re.search(
        r'<meta[^>]*http-equiv\s*=\s*["\']?refresh["\']?[^>]*content\s*=\s*["\'][^"\']*;\s*url\s*=\s*([^"\']+)["\']',
        body,
        flags=re.IGNORECASE,
    )
    if meta_refresh:
        return html.unescape(meta_refresh.group(1).strip().strip("'\""))

    content_first = re.search(
        r'<meta[^>]*content\s*=\s*["\'][^"\']*;\s*url\s*=\s*([^"\']+)["\'][^>]*http-equiv\s*=\s*["\']?refresh["\']?',
        body,
        flags=re.IGNORECASE,
    )
    if content_first:
        return html.unescape(content_first.group(1).strip().strip("'\""))

    js_redirect = re.search(
        r"""(?:window\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']""",
        body,
        flags=re.IGNORECASE,
    )
    if js_redirect:
        return html.unescape(js_redirect.group(1).strip())

    js_replace = re.search(
        r"""(?:window\.)?location\.replace\(\s*["']([^"']+)["']\s*\)""",
        body,
        flags=re.IGNORECASE,
    )
    if js_replace:
        return html.unescape(js_replace.group(1).strip())

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
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    max_redirects = 10
    max_html_redirects = 3
    html_redirect_hops = 0
    current_url = normalized
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
            for redirect_count in range(max_redirects + 1):
                host, path = _validate_redirect_target(current_url, allow_short_hosts=True)
                if host in _SHORT_HOSTS and not _is_supported_short_path(host, path):
                    raise UnsupportedLocationInputError("This Google short link format is not supported.")
                response = client.get(current_url)
                location = response.headers.get("location")
                if response.is_redirect or response.is_informational:
                    if not location:
                        details = _format_resolution_event_details(
                            stage="redirect_missing_location",
                            short_url=normalized,
                            current_url=current_url,
                            response_url=str(response.url),
                            status_code=response.status_code,
                            redirect_hop=redirect_count,
                        )
                        raise ShortUrlResolutionError(
                            "Redirect response from short link did not include a Location header. "
                            f"Event details: {details}."
                        )
                    current_url = urljoin(str(response.url), location)
                    continue
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else "unknown"
                    details = _format_resolution_event_details(
                        stage="http_error_status",
                        short_url=normalized,
                        current_url=current_url,
                        response_url=str(response.url),
                        status_code=status,
                        redirect_hop=redirect_count,
                    )
                    raise ShortUrlResolutionError(
                        f"Google Maps short link returned HTTP {status} while resolving redirects. "
                        f"Event details: {details}."
                    ) from exc
                final_url = str(response.url)
                if not final_url:
                    details = _format_resolution_event_details(
                        stage="empty_final_url",
                        short_url=normalized,
                        current_url=current_url,
                        response_url=str(response.url),
                        status_code=response.status_code,
                        redirect_hop=redirect_count,
                    )
                    raise ShortUrlResolutionError(
                        "Google Maps short link resolved to an empty URL. "
                        f"Event details: {details}."
                    )
                _validate_redirect_target(final_url, allow_short_hosts=False)
                try:
                    extract_lat_lng_from_google_maps_url(final_url)
                    return final_url
                except MalformedLocationInputError:
                    pass
                html_redirect_url = _extract_html_redirect_url(getattr(response, "text", None))
                if html_redirect_url:
                    html_redirect_hops += 1
                    if html_redirect_hops > max_html_redirects:
                        raise MalformedLocationInputError(
                            "Resolved page appears to be stuck in recursive HTML redirects; "
                            "could not reach a stable Google Maps destination."
                        )
                    current_url = urljoin(final_url, html_redirect_url)
                    _validate_redirect_target(current_url, allow_short_hosts=False)
                    continue

                html_pair = _extract_lat_lng_from_google_maps_html(getattr(response, "text", None))
                if html_pair:
                    lat, lng, _ = html_pair
                    if _is_likely_google_block_page_coordinate_pair(lat, lng):
                        raise MalformedLocationInputError(
                            "Resolved page appears to be blocked/bot-protection content from Google; "
                            "could not extract destination coordinates."
                        )
                    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
                return final_url
            raise ShortUrlResolutionError(f"Google Maps short link exceeded redirect limit ({max_redirects}).")
    except httpx.TimeoutException as exc:
        raise ShortUrlResolutionError(
            "Google Maps short link resolution timed out. Please check your connection and try again."
        ) from exc
    except httpx.RequestError as exc:
        raise ShortUrlResolutionError(
            "Failed to resolve the Google Maps short link due to a network/protocol error."
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
        return parse_decimal_pair(normalized)

    if "°" in normalized and re.search(r"[NSEW]", normalized, re.IGNORECASE):
        return parse_degree_pair(normalized)

    if re.match(r"^https?://", normalized, re.IGNORECASE):
        return parse_google_maps_url(normalized)

    raise UnsupportedLocationInputError("Unsupported location input format.")
