from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class LocationInputClassification:
    input_format: str
    confidence: float
    parse_status: str
    input_length: int
    input_host: str | None
    has_url: bool
    has_coordinates: bool
    coordinate_source: str | None
    normalized_lat: float | None
    normalized_lng: float | None
    failure_reason: str | None


_DECIMAL_PAIR_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*[,\s]\s*([+-]?\d+(?:\.\d+)?)")
_MIXED_DECIMAL_RE = re.compile(r"(?<!\d)([+-]?\d{1,3}(?:\.\d+)?)[,\s]+([+-]?\d{1,3}(?:\.\d+)?)(?!\d)")
_DMS_COMPONENT_RE = re.compile(
    r"(?P<deg>\d{1,3})\D+(?P<min>\d{1,2})\D+(?P<sec>\d{1,2}(?:\.\d+)?)\D*(?P<hem>[NSEW])",
    re.IGNORECASE,
)
_DM_COMPONENT_RE = re.compile(
    r"(?P<deg>\d{1,3})\D+(?P<min>\d{1,2}(?:\.\d+)?)\D*(?P<hem>[NSEW])",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _mk(
    *,
    input_format: str,
    confidence: float,
    parse_status: str,
    input_length: int,
    input_host: str | None,
    has_url: bool,
    has_coordinates: bool,
    coordinate_source: str | None = None,
    normalized_lat: float | None = None,
    normalized_lng: float | None = None,
    failure_reason: str | None = None,
) -> LocationInputClassification:
    return LocationInputClassification(
        input_format=input_format,
        confidence=confidence,
        parse_status=parse_status,
        input_length=input_length,
        input_host=input_host,
        has_url=has_url,
        has_coordinates=has_coordinates,
        coordinate_source=coordinate_source,
        normalized_lat=normalized_lat,
        normalized_lng=normalized_lng,
        failure_reason=failure_reason,
    )


def _validate_coordinates(lat: float, lng: float) -> tuple[bool, str | None]:
    if lat < -90 or lat > 90 or lng < -180 or lng > 180:
        return False, "invalid_coordinate_range"
    return True, None


def _parse_decimal_pair(text: str) -> tuple[float, float] | None:
    direct = text.strip()
    if re.fullmatch(r"(?i)(lat(?:itude)?\s*[:=]?\s*)?[+-]?\d+(?:\.\d+)?\s*[,\s]\s*(lng|lon|longitude)?\s*[:=]?\s*[+-]?\d+(?:\.\d+)?", direct):
        numbers = re.findall(r"[+-]?\d+(?:\.\d+)?", direct)
        if len(numbers) >= 2:
            return float(numbers[0]), float(numbers[1])
    match = re.fullmatch(_DECIMAL_PAIR_RE, direct)
    if match:
        return float(match.group(1)), float(match.group(2))
    return None


def _extract_decimal_pair(text: str) -> tuple[float, float] | None:
    match = _MIXED_DECIMAL_RE.search(text)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _parse_dms_or_dm(text: str) -> tuple[str, float, float] | None:
    dms_parts = list(_DMS_COMPONENT_RE.finditer(text))
    if len(dms_parts) == 2:
        vals: dict[str, float] = {}
        for part in dms_parts:
            deg = float(part.group("deg"))
            mins = float(part.group("min"))
            secs = float(part.group("sec"))
            hem = part.group("hem").upper()
            if mins >= 60 or secs >= 60:
                return None
            value = deg + mins / 60 + secs / 3600
            if hem in {"S", "W"}:
                value *= -1
            vals["lat" if hem in {"N", "S"} else "lng"] = value
        if "lat" in vals and "lng" in vals:
            return "dms_coordinates", vals["lat"], vals["lng"]

    dm_parts = list(_DM_COMPONENT_RE.finditer(text))
    if len(dm_parts) == 2:
        vals2: dict[str, float] = {}
        for part in dm_parts:
            deg = float(part.group("deg"))
            mins = float(part.group("min"))
            hem = part.group("hem").upper()
            if mins >= 60:
                return None
            value = deg + mins / 60
            if hem in {"S", "W"}:
                value *= -1
            vals2["lat" if hem in {"N", "S"} else "lng"] = value
        if "lat" in vals2 and "lng" in vals2:
            return "dm_coordinates", vals2["lat"], vals2["lng"]
    return None


def _is_google_host(host: str) -> bool:
    return host == "google.com" or host.endswith(".google.com")


def _google_input_format(path: str) -> str:
    if "/maps/place/" in path:
        return "google_maps_place_url"
    if "/maps/search/" in path:
        return "google_maps_search_url"
    return "google_maps_full_url"


def _extract_url(raw: str) -> tuple[str, str] | None:
    m = _URL_RE.search(raw)
    if not m:
        return None
    url = m.group(0).rstrip(").,;]}")
    parsed = urlparse(url)
    return url, parsed.netloc.lower()


def classify_location_input(raw_input: str) -> LocationInputClassification:
    raw = (raw_input or "").replace("\u00A0", " ").strip()
    raw = raw[:1000]
    input_length = len(raw)

    if not raw:
        return _mk(
            input_format="empty",
            confidence=1.0,
            parse_status="empty",
            input_length=0,
            input_host=None,
            has_url=False,
            has_coordinates=False,
            failure_reason="empty_input",
        )

    if raw.lower().startswith("geo:"):
        payload = raw[4:]
        pair = _parse_decimal_pair(payload)
        if pair:
            ok, reason = _validate_coordinates(pair[0], pair[1])
            if ok:
                return _mk(
                    input_format="geo_uri",
                    confidence=0.98,
                    parse_status="parsed",
                    input_length=input_length,
                    input_host=None,
                    has_url=False,
                    has_coordinates=True,
                    coordinate_source="geo_uri",
                    normalized_lat=pair[0],
                    normalized_lng=pair[1],
                )
            return _mk(
                input_format="geo_uri",
                confidence=0.98,
                parse_status="invalid_coordinates",
                input_length=input_length,
                input_host=None,
                has_url=False,
                has_coordinates=False,
                failure_reason=reason,
            )

    found = _extract_url(raw)
    if found:
        url, host = found
        parsed = urlparse(url)
        path = parsed.path or "/"
        query = parse_qs(parsed.query)

        if host == "maps.app.goo.gl" or (host == "goo.gl" and (path == "/maps" or path.startswith("/maps/"))):
            return _mk(
                input_format="google_maps_short_url",
                confidence=1.0,
                parse_status="recognized_not_resolved",
                input_length=input_length,
                input_host=host,
                has_url=True,
                has_coordinates=False,
                failure_reason="short_url_not_resolved",
            )

        if _is_google_host(host):
            input_format = _google_input_format(path)
            at_match = re.search(r"@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", parsed.path)
            if at_match:
                lat, lng = float(at_match.group(1)), float(at_match.group(2))
                ok, reason = _validate_coordinates(lat, lng)
                if ok:
                    return _mk(
                        input_format="google_maps_at_url",
                        confidence=1.0,
                        parse_status="parsed",
                        input_length=input_length,
                        input_host=host,
                        has_url=True,
                        has_coordinates=True,
                        coordinate_source="google_maps_at_segment",
                        normalized_lat=lat,
                        normalized_lng=lng,
                    )
                return _mk(
                    input_format="google_maps_at_url",
                    confidence=1.0,
                    parse_status="invalid_coordinates",
                    input_length=input_length,
                    input_host=host,
                    has_url=True,
                    has_coordinates=False,
                    failure_reason=reason,
                )

            tokens = re.search(r"!3d([+-]?\d+(?:\.\d+)?)!4d([+-]?\d+(?:\.\d+)?)", url)
            if tokens:
                lat, lng = float(tokens.group(1)), float(tokens.group(2))
                ok, reason = _validate_coordinates(lat, lng)
                if ok:
                    return _mk(
                        input_format=input_format,
                        confidence=0.99,
                        parse_status="parsed",
                        input_length=input_length,
                        input_host=host,
                        has_url=True,
                        has_coordinates=True,
                        coordinate_source="google_maps_3d4d_tokens",
                        normalized_lat=lat,
                        normalized_lng=lng,
                    )
                return _mk(
                    input_format=input_format,
                    confidence=0.99,
                    parse_status="invalid_coordinates",
                    input_length=input_length,
                    input_host=host,
                    has_url=True,
                    has_coordinates=False,
                    failure_reason=reason,
                )

            for key in ("query", "q", "ll"):
                vals = query.get(key)
                if vals:
                    pair = _extract_decimal_pair(vals[0])
                    if pair:
                        ok, reason = _validate_coordinates(pair[0], pair[1])
                        if ok:
                            return _mk(
                                input_format="google_maps_search_url" if key in {"query", "q"} else input_format,
                                confidence=0.9,
                                parse_status="parsed",
                                input_length=input_length,
                                input_host=host,
                                has_url=True,
                                has_coordinates=True,
                                coordinate_source="google_maps_query_param",
                                normalized_lat=pair[0],
                                normalized_lng=pair[1],
                            )
                        return _mk(
                            input_format=input_format,
                            confidence=0.9,
                            parse_status="invalid_coordinates",
                            input_length=input_length,
                            input_host=host,
                            has_url=True,
                            has_coordinates=False,
                            failure_reason=reason,
                        )

            return _mk(
                input_format=input_format,
                confidence=0.9,
                parse_status="unknown",
                input_length=input_length,
                input_host=host,
                has_url=True,
                has_coordinates=False,
                failure_reason="no_coordinates_found",
            )

        if host == "maps.apple.com":
            ll_val = (query.get("ll") or [None])[0]
            pair = _extract_decimal_pair(ll_val or "") if ll_val else None
            if pair:
                ok, reason = _validate_coordinates(pair[0], pair[1])
                if ok:
                    return _mk(
                        input_format="apple_maps_url",
                        confidence=1.0,
                        parse_status="parsed",
                        input_length=input_length,
                        input_host=host,
                        has_url=True,
                        has_coordinates=True,
                        coordinate_source="apple_maps_ll_param",
                        normalized_lat=pair[0],
                        normalized_lng=pair[1],
                    )
                return _mk(
                    input_format="apple_maps_url",
                    confidence=1.0,
                    parse_status="invalid_coordinates",
                    input_length=input_length,
                    input_host=host,
                    has_url=True,
                    has_coordinates=False,
                    failure_reason=reason,
                )
            return _mk(
                input_format="apple_maps_url",
                confidence=0.95,
                parse_status="unknown",
                input_length=input_length,
                input_host=host,
                has_url=True,
                has_coordinates=False,
                failure_reason="no_coordinates_found",
            )

        if host in {"openstreetmap.org", "www.openstreetmap.org"}:
            mlat = (query.get("mlat") or [None])[0]
            mlon = (query.get("mlon") or [None])[0]
            if mlat and mlon:
                lat, lng = float(mlat), float(mlon)
                ok, reason = _validate_coordinates(lat, lng)
                if ok:
                    return _mk(
                        input_format="openstreetmap_url",
                        confidence=1.0,
                        parse_status="parsed",
                        input_length=input_length,
                        input_host=host,
                        has_url=True,
                        has_coordinates=True,
                        coordinate_source="osm_mlat_mlon_params",
                        normalized_lat=lat,
                        normalized_lng=lng,
                    )
                return _mk(
                    input_format="openstreetmap_url",
                    confidence=1.0,
                    parse_status="invalid_coordinates",
                    input_length=input_length,
                    input_host=host,
                    has_url=True,
                    has_coordinates=False,
                    failure_reason=reason,
                )
            return _mk(
                input_format="openstreetmap_url",
                confidence=0.95,
                parse_status="unknown",
                input_length=input_length,
                input_host=host,
                has_url=True,
                has_coordinates=False,
                failure_reason="no_coordinates_found",
            )

        if host in {"waze.com", "www.waze.com"}:
            ll_val = (query.get("ll") or [None])[0]
            pair = _extract_decimal_pair(ll_val or "") if ll_val else None
            if pair:
                ok, reason = _validate_coordinates(pair[0], pair[1])
                if ok:
                    return _mk(
                        input_format="waze_url",
                        confidence=1.0,
                        parse_status="parsed",
                        input_length=input_length,
                        input_host=host,
                        has_url=True,
                        has_coordinates=True,
                        coordinate_source="waze_ll_param",
                        normalized_lat=pair[0],
                        normalized_lng=pair[1],
                    )
                return _mk(
                    input_format="waze_url",
                    confidence=1.0,
                    parse_status="invalid_coordinates",
                    input_length=input_length,
                    input_host=host,
                    has_url=True,
                    has_coordinates=False,
                    failure_reason=reason,
                )
            return _mk(
                input_format="waze_url",
                confidence=0.95,
                parse_status="unknown",
                input_length=input_length,
                input_host=host,
                has_url=True,
                has_coordinates=False,
                failure_reason="no_coordinates_found",
            )

        return _mk(
            input_format="unknown_url",
            confidence=0.8,
            parse_status="unknown",
            input_length=input_length,
            input_host=host,
            has_url=True,
            has_coordinates=False,
            failure_reason="unsupported_location_format",
        )

    dms_or_dm = _parse_dms_or_dm(raw)
    if dms_or_dm:
        input_format, lat, lng = dms_or_dm
        ok, reason = _validate_coordinates(lat, lng)
        if ok:
            return _mk(
                input_format=input_format,
                confidence=0.98,
                parse_status="parsed",
                input_length=input_length,
                input_host=None,
                has_url=False,
                has_coordinates=True,
                coordinate_source="raw_text_dms" if input_format == "dms_coordinates" else "raw_text_dm",
                normalized_lat=lat,
                normalized_lng=lng,
            )
        return _mk(
            input_format=input_format,
            confidence=0.98,
            parse_status="invalid_coordinates",
            input_length=input_length,
            input_host=None,
            has_url=False,
            has_coordinates=False,
            failure_reason=reason,
        )

    pair = _parse_decimal_pair(raw)
    if pair:
        ok, reason = _validate_coordinates(pair[0], pair[1])
        if ok:
            return _mk(
                input_format="decimal_coordinates",
                confidence=0.98,
                parse_status="parsed",
                input_length=input_length,
                input_host=None,
                has_url=False,
                has_coordinates=True,
                coordinate_source="raw_text_decimal",
                normalized_lat=pair[0],
                normalized_lng=pair[1],
            )
        return _mk(
            input_format="decimal_coordinates",
            confidence=0.98,
            parse_status="invalid_coordinates",
            input_length=input_length,
            input_host=None,
            has_url=False,
            has_coordinates=False,
            failure_reason=reason,
        )

    mixed_pair = _extract_decimal_pair(raw)
    if mixed_pair:
        ok, reason = _validate_coordinates(mixed_pair[0], mixed_pair[1])
        if ok:
            return _mk(
                input_format="mixed_text_with_coordinates",
                confidence=0.92,
                parse_status="parsed",
                input_length=input_length,
                input_host=None,
                has_url=False,
                has_coordinates=True,
                coordinate_source="mixed_text_decimal",
                normalized_lat=mixed_pair[0],
                normalized_lng=mixed_pair[1],
            )
        return _mk(
            input_format="mixed_text_with_coordinates",
            confidence=0.92,
            parse_status="invalid_coordinates",
            input_length=input_length,
            input_host=None,
            has_url=False,
            has_coordinates=False,
            failure_reason=reason,
        )

    if re.fullmatch(r"[23456789CFGHJMPQRVWX]{2,8}\+[23456789CFGHJMPQRVWX]{2,5}(?:\s+.+)?", raw, re.IGNORECASE):
        return _mk(
            input_format="plus_code",
            confidence=0.9,
            parse_status="recognized_unsupported",
            input_length=input_length,
            input_host=None,
            has_url=False,
            has_coordinates=False,
            failure_reason="unsupported_location_format",
        )

    if re.fullmatch(r"/?/?/?[A-Za-z]+\.[A-Za-z]+\.[A-Za-z]+", raw):
        return _mk(
            input_format="what3words",
            confidence=0.9,
            parse_status="recognized_unsupported",
            input_length=input_length,
            input_host=None,
            has_url=False,
            has_coordinates=False,
            failure_reason="unsupported_location_format",
        )

    if any(ch.isdigit() for ch in raw) and ("," in raw or len(raw.split()) >= 3):
        return _mk(
            input_format="plain_address",
            confidence=0.65,
            parse_status="recognized_unsupported",
            input_length=input_length,
            input_host=None,
            has_url=False,
            has_coordinates=False,
            failure_reason="plain_address_unsupported",
        )

    return _mk(
        input_format="unknown_text",
        confidence=0.35,
        parse_status="unknown",
        input_length=input_length,
        input_host=None,
        has_url=False,
        has_coordinates=False,
        failure_reason="no_coordinates_found",
    )
