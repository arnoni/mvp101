from __future__ import annotations

from dataclasses import dataclass
import hashlib
import random
from typing import Literal
import ipaddress
import re
from urllib.parse import urlparse, parse_qs, unquote, urlencode, urlunparse

import httpx
import structlog

MAX_LOCATION_INPUT_LEN = 2048
INPUT_KIND = Literal["decimal_pair", "degree_pair", "google_maps_url", "google_maps_short_url"]
_SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl", "g.page"}
logger = structlog.get_logger(__name__)
_BLOCKED_RESOLUTION_MESSAGE = (
    "This Google Maps short link could not be resolved automatically. Please paste the full Google Maps URL, "
    "or type-in the coordinates."
)
_SHORT_URL_CACHE_TTL_SECONDS = 30 * 24 * 3600
_SHORT_URL_STRIPPED_CACHE_PARAMS = {"g_st", "g_st_aw"}
_RESOLVED_SHORT_URL_STRIPPED_QUERY_PARAMS = {"g_st", "g_st_aw"}
_PLACE_PAGE_NO_COORDS_MESSAGE = (
    "This Google Maps link points to a place page that does not expose coordinates. "
    "Please paste the full Google Maps URL or type the coordinates directly."
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


def _build_short_url_cache_key_input(normalized_url: str) -> str:
    parsed = urlparse(normalized_url)
    host = (parsed.hostname or "").lower()
    if host != "maps.app.goo.gl":
        return normalized_url
    q = parse_qs(parsed.query, keep_blank_values=True)
    kept: list[tuple[str, str]] = []
    for key in sorted(q.keys()):
        if key in _SHORT_URL_STRIPPED_CACHE_PARAMS:
            continue
        for value in q[key]:
            kept.append((key, value))
    canonical = parsed._replace(query=urlencode(kept, doseq=True))
    return urlunparse(canonical)


def _strip_tracking_query_params(url: str, *, keys: set[str]) -> str:
    parsed = urlparse(url)
    q = parse_qs(parsed.query, keep_blank_values=True)
    kept: list[tuple[str, str]] = []
    for key in sorted(q.keys()):
        if key in keys:
            continue
        for value in q[key]:
            kept.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(kept, doseq=True)))


def _detect_resolved_url_format(url: str) -> str:
    decoded = unquote(url)
    query = parse_qs(urlparse(url).query)
    if _extract_pair(query.get("q", [None])[0]):
        return "query_q"
    if re.search(r"!3d([+-]?\d+(?:\.\d+)?)!4d([+-]?\d+(?:\.\d+)?)", decoded):
        return "place_3d4d"
    if re.search(r"@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", decoded):
        return "viewport"
    if re.search(r"!1s0x[0-9a-f]+:0x[0-9a-f]+", decoded, re.IGNORECASE):
        return "place_id_only"
    return "unknown"


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

    try:
        dms_parsed = parse_degree_pair(decoded)
        return dms_parsed.latitude, dms_parsed.longitude, "decoded_dms"
    except MalformedLocationInputError:
        pass

    raise MalformedLocationInputError(
        "Could not extract coordinates from the resolved Google Maps URL. "
        "The link may point to a place page without explicit coordinates."
    )


async def resolve_google_maps_short_url_async(
    raw: str,
    *,
    redis_client=None,
    http_client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 4.0,
) -> str:
    normalized = _normalize_raw(raw)
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https":
        raise UnsupportedLocationInputError("Only HTTPS Google Maps short links are supported.")
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

    cache_key_input = _build_short_url_cache_key_input(normalized)
    digest = hashlib.sha256(cache_key_input.encode("utf-8")).hexdigest()[:16]
    cache_key = f"maps:expand:{digest}"
    if redis_client:
        try:
            cached = await redis_client.get(cache_key)
            if isinstance(cached, str) and cached.startswith("https://"):
                try:
                    extract_lat_lng_from_google_maps_url(cached)
                    logger.info("maps_short_url_cache_hit", host=host, url_hash=digest)
                    return cached
                except MalformedLocationInputError:
                    logger.info("maps_short_url_cache_invalid_coordinate_free", host=host, url_hash=digest)
                    cached = None
            logger.info("maps_short_url_cache_miss", host=host, url_hash=digest)
        except Exception:
            logger.warning("maps_short_url_cache_read_failed", host=host, url_hash=digest, exc_info=True)

    try:
        client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=3.0),
            follow_redirects=True,
            headers=headers,
            max_redirects=8,
        )
        owns_client = http_client is None
        response = await client.get(
            normalized,
            follow_redirects=True,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, connect=3.0),
        )
        response.raise_for_status()

        for hop in response.history:
            hop_url = str(hop.url)
            hop_host, hop_path = _validate_redirect_target(hop_url, allow_short_hosts=True)
            if hop_host in _SHORT_HOSTS and not _is_supported_short_path(hop_host, hop_path):
                raise UnsupportedLocationInputError("This Google short link format is not supported.")

        final_url = str(response.url)
        final_host, _ = _validate_redirect_target(final_url, allow_short_hosts=False)
        
        blocked_markers = ("consent.google.com", "/sorry/")
        if final_host == "consent.google.com" or any(marker in final_url for marker in blocked_markers):
            _log_attempt(False, "short_url_redirect_failed", response.status_code)
            raise LocationResolutionBlockedError(_BLOCKED_RESOLUTION_MESSAGE)
        _log_attempt(True, None, response.status_code)
        if redis_client:
            try:
                extract_lat_lng_from_google_maps_url(final_url)
                ttl = int(_SHORT_URL_CACHE_TTL_SECONDS * (1 + random.uniform(-0.05, 0.05)))
                await redis_client.setex(cache_key, ttl, final_url)
            except MalformedLocationInputError:
                logger.info("maps_short_url_cache_skip_coordinate_free", host=host, url_hash=digest)
            except Exception:
                logger.warning("maps_short_url_cache_write_failed", host=host, url_hash=digest, exc_info=True)
        return final_url
    except httpx.TimeoutException as exc:
        _log_attempt(False, "short_url_timeout", None)
        raise ShortUrlResolutionError(
            "Google Maps short link resolution timed out. Please check your connection and try again."
        ) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        _log_attempt(False, "short_url_redirect_failed", status)
        raise ShortUrlResolutionError(
            f"Google Maps short link returned HTTP {status} while resolving redirects."
        ) from exc
    except httpx.RequestError as exc:
        _log_attempt(False, "short_url_redirect_failed", None)
        raise ShortUrlResolutionError(
            "Failed to resolve the Google Maps short link due to a network/protocol error."
        ) from exc
    except (LocationResolutionBlockedError, ShortUrlResolutionError, UnsupportedLocationInputError):
        raise
    except Exception as exc:
        _log_attempt(False, "short_url_redirect_failed", None)
        raise ShortUrlResolutionError(
            "Failed to resolve the Google Maps short link due to an unexpected resolver error."
        ) from exc
    finally:
        if 'owns_client' in locals() and owns_client:
            await client.aclose()


def resolve_google_maps_short_url(raw: str, timeout_seconds: float = 4.0) -> str:
    raise ShortUrlResolutionError("Synchronous short URL resolution is not supported; use async parser flow.")


def parse_google_maps_url(raw: str) -> ParsedLocationInput:
    normalized = _normalize_raw(raw)
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if not _is_supported_google_host(host):
        raise UnsupportedLocationInputError("Only Google Maps URLs are supported.")
    if host in _SHORT_HOSTS:
        raise UnsupportedLocationInputError("Short URLs require async parser flow.")
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


async def parse_google_maps_url_async(raw: str, *, redis_client=None, http_client: httpx.AsyncClient | None = None) -> ParsedLocationInput:
    normalized = _normalize_raw(raw)
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    if not _is_supported_google_host(host):
        raise UnsupportedLocationInputError("Only Google Maps URLs are supported.")

    if host in _SHORT_HOSTS:
        input_url_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        resolution_log: dict[str, object] = {
            "input_url_hash": input_url_hash,
            "had_query_params": bool(parsed.query),
            "short_url_host": host,
            "redis_cache_hit": None,
            "final_url_hash": None,
            "final_url_host": None,
            "redirect_hop_count": None,
            "resolved_url_format": "unknown",
            "resolution_result": "failed",
            "error_code": None,
            "parser_branch": "parse_google_maps_url_async_short_url",
            "parsed_lat": None,
            "parsed_lon": None,
        }
        def _emit_resolution_log() -> None:
            level = "info" if resolution_log.get("resolution_result") == "success" else "warning"
            getattr(logger, level)("google_maps_short_url_resolution_attempted", **resolution_log)
        if not _is_supported_short_path(host, parsed.path):
            resolution_log["error_code"] = "UNSUPPORTED_LOCATION_INPUT"
            _emit_resolution_log()
            raise UnsupportedLocationInputError("Only Google Maps short URLs are supported.")
        cache_key_digest = hashlib.sha256(_build_short_url_cache_key_input(normalized).encode("utf-8")).hexdigest()[:16]
        cache_key = f"maps:expand:{cache_key_digest}"
        resolution_log["redis_cache_hit"] = None
        if redis_client:
            try:
                cached = await redis_client.get(cache_key)
                resolution_log["redis_cache_hit"] = isinstance(cached, str) and cached.startswith("https://")
            except Exception:
                resolution_log["redis_cache_hit"] = None
        try:
            resolved = await resolve_google_maps_short_url_async(normalized, redis_client=redis_client, http_client=http_client)
        except LocationResolutionBlockedError:
            resolution_log["resolution_result"] = "blocked"
            resolution_log["error_code"] = "SHORT_URL_RESOLUTION_BLOCKED"
            _emit_resolution_log()
            raise
        except ShortUrlResolutionError as exc:
            resolution_log["resolution_result"] = "timeout" if "timed out" in str(exc).lower() else "failed"
            resolution_log["error_code"] = "SHORT_URL_RESOLUTION_FAILED"
            _emit_resolution_log()
            raise
        except UnsupportedLocationInputError:
            resolution_log["resolution_result"] = "failed"
            resolution_log["error_code"] = "UNSUPPORTED_LOCATION_INPUT"
            _emit_resolution_log()
            raise
        resolution_log["final_url_hash"] = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
        resolution_log["final_url_host"] = (urlparse(resolved).hostname or "").lower()
        resolution_log["resolved_url_format"] = _detect_resolved_url_format(resolved)
        try:
            resolved_for_parse = _strip_tracking_query_params(
                resolved, keys=_RESOLVED_SHORT_URL_STRIPPED_QUERY_PARAMS
            )
            lat, lng, method = extract_lat_lng_from_google_maps_url(resolved_for_parse)
        except MalformedLocationInputError as exc:
            resolved_host = (urlparse(resolved).hostname or "").lower()
            resolved_digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
            logger.info(
                "location_resolve_attempt",
                input_type="google_short_url",
                resolver_strategy="redirect_follow",
                success=False,
                failure_reason="short_url_resolved_but_parse_failed",
                resolved_host=resolved_host,
                resolved_url_hash=resolved_digest,
                parser_path_used="existing_google_maps_parser",
            )
            details = _format_resolution_event_details(
                stage="extract_coordinates_failed_after_resolution",
                short_url=normalized,
                final_url=resolved,
            )
            if resolution_log["resolved_url_format"] == "place_id_only":
                resolution_log["resolution_result"] = "place_page_without_coordinates"
                resolution_log["error_code"] = "RESOLVED_PLACE_PAGE_NO_COORDS"
                _emit_resolution_log()
                raise MalformedLocationInputError(_PLACE_PAGE_NO_COORDS_MESSAGE) from exc
            resolution_log["resolution_result"] = "non_parseable"
            resolution_log["error_code"] = "MALFORMED_LOCATION_INPUT"
            _emit_resolution_log()
            raise MalformedLocationInputError(
                "Could not extract coordinates from the resolved Google Maps short URL. "
                f"Event details: {details}."
            ) from exc
        resolution_log["resolution_result"] = "success"
        resolution_log["parsed_lat"] = lat
        resolution_log["parsed_lon"] = lng
        _emit_resolution_log()
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


async def parse_location_input_async(raw: str, *, redis_client=None, http_client: httpx.AsyncClient | None = None) -> ParsedLocationInput:
    normalized = _normalize_raw(raw)
    _validate_raw(normalized)
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?", normalized) or re.fullmatch(
        r"[+-]?\d+(?:\.\d+)?\s+[+-]?\d+(?:\.\d+)?", normalized
    ):
        parsed = parse_decimal_pair(normalized)
    elif "°" in normalized and re.search(r"[NSEW]", normalized, re.IGNORECASE):
        parsed = parse_degree_pair(normalized)
    elif re.match(r"^https?://", normalized, re.IGNORECASE):
        parsed_url = urlparse(normalized)
        host = (parsed_url.hostname or "").lower()
        if host in _SHORT_HOSTS:
            parsed = await parse_google_maps_url_async(normalized, redis_client=redis_client, http_client=http_client)
        else:
            parsed = parse_google_maps_url(normalized)
    else:
        raise UnsupportedLocationInputError("Unsupported location input format.")
    if not (15.9 <= parsed.latitude <= 16.3 and 107.8 <= parsed.longitude <= 108.4):
        raise LocationNotSupportedError("This location is not supported currently")
    return parsed
