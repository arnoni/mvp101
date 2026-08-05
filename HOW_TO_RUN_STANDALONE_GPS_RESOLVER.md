# How to run the standalone GPS resolver

The resolver reads one location input and prints the resolved latitude and longitude as JSON. It
supports decimal coordinates, DMS coordinates, full Google Maps URLs, and supported Google Maps
shared links.

## 1. Prerequisite

Install `uv`, then keep `standalone_gps_resolver.py` in any folder you can access from a terminal.
The script contains its own Python and dependency requirements, so `uv` installs the required
packages automatically on the first run.

Open PowerShell, Command Prompt, Terminal, or another shell in the folder containing the script.

## 2. Basic command

PowerShell or Windows Command Prompt:

```text
uv run standalone_gps_resolver.py "LOCATION_INPUT"
```

macOS or Linux with Bash/Zsh:

```text
uv run standalone_gps_resolver.py 'LOCATION_INPUT'
```

Always quote the complete input. Quoting is especially important for URLs containing `&` and for
coordinate pairs containing spaces. The examples below use Windows-compatible double quotes. In
Bash or Zsh, replace the surrounding double quotes with single quotes, especially when a URL
contains `!`, which an interactive shell may interpret as history expansion.

Display the built-in help:

```text
uv run standalone_gps_resolver.py --help
```

## 3. Full Google Maps URL examples

These full URLs are parsed locally and do not require a Google API key.

### Search/query URL

```text
uv run standalone_gps_resolver.py "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022"
```

Expected resolution method: `query_query`.

The current parser recognizes coordinate pairs in these query parameters, in this order:

1. `q`
2. `ll`
3. `query`
4. `center`
5. `destination`
6. `origin`
7. `saddr`
8. `daddr`

If one URL contains multiple recognized parameters, the first matching parameter in this list is
used.

### Place-data URL (`!3d...!4d...`)

```text
uv run standalone_gps_resolver.py "https://www.google.com/maps/place/example/@16.1000,108.1000,17z/data=!3d16.0544!4d108.2022"
```

Expected resolution method: `place_3d4d`. The place-data coordinates are used instead of the
`@16.1000,108.1000` viewport coordinates.

### Reverse place-data URL (`!2d...!3d...`)

```text
uv run standalone_gps_resolver.py "https://www.google.com/maps/place/example/data=!2d108.2022!3d16.0544"
```

Expected resolution method: `place_2d3d`. In this Google URL form, longitude appears before
latitude.

### Viewport URL (`@lat,lng`)

```text
uv run standalone_gps_resolver.py "https://www.google.com/maps/place/example/@16.0544,108.2022,17z"
```

Expected resolution method: `viewport_center`.

Important: viewport coordinates can describe the center of the visible map rather than the exact
venue entrance or selected place pin.

### Directions URL

```text
uv run standalone_gps_resolver.py "https://www.google.com/maps/dir/?api=1&origin=16.0500%2C108.2000&destination=16.0600%2C108.2100"
```

Expected resolution method: `query_destination`, because `destination` has priority over `origin`
in the current parser. Route coordinates are route endpoints and may not represent a venue pin.

### URL containing encoded DMS coordinates

```text
uv run standalone_gps_resolver.py "https://www.google.com/maps/place/16%C2%B003%2715.8%22N+108%C2%B012%2707.9%22E"
```

Expected resolution method: `decoded_dms`.

## 4. Google Maps shared-link examples

Shared links require an internet connection because the script follows Google's redirects before
extracting coordinates from the resulting full URL. Replace the example identifier with the actual
shared link you received.

### Current Google Maps share link

```text
uv run standalone_gps_resolver.py "https://maps.app.goo.gl/SHARE_ID"
```

### Legacy `goo.gl/maps` share link

```text
uv run standalone_gps_resolver.py "https://goo.gl/maps/SHARE_ID"
```

### Google Business Profile `g.page` link

```text
uv run standalone_gps_resolver.py "https://g.page/PLACE_NAME"
```

Only HTTPS shared links are accepted. Short-link resolution can fail because of a timeout, consent
page, bot protection, rate limiting, a changed redirect format, or a place page that does not expose
coordinates.

Diagnostic log lines may appear before the final JSON when resolving a shared link. This is current
exported behavior.

## 5. Direct coordinate examples

### Latitude, longitude

```text
uv run standalone_gps_resolver.py "16.0544, 108.2022"
```

### Space-separated coordinates

```text
uv run standalone_gps_resolver.py "16.0544 108.2022"
```

### Unambiguously reversed longitude, latitude

```text
uv run standalone_gps_resolver.py "108.2022, 16.0544"
```

Expected resolution method: `decimal_pair_reversed`.

### DMS coordinates

The existing DMS parser accepts non-letter separators. Colons avoid quote escaping and Windows
console issues while keeping the required degree symbol and hemisphere letters:

```text
uv run standalone_gps_resolver.py "16°03:15.8N 108°12:07.9E"
```

In Bash or Zsh, single quotes can be used around the same DMS value. Traditional input such as
`16°03'15.8"N 108°12'07.9"E` is also supported, but it requires shell-specific quote escaping.

Expected resolution method: `degree_pair`.

## 6. Read input from a pipe

The positional argument can be omitted when input is supplied through standard input.

PowerShell:

```text
"16.0544, 108.2022" | uv run standalone_gps_resolver.py
```

Bash or Zsh:

```text
printf '%s\n' '16.0544, 108.2022' | uv run standalone_gps_resolver.py
```

Windows Command Prompt:

```text
echo 16.0544, 108.2022 | uv run standalone_gps_resolver.py
```

## 7. Understanding successful output

Example:

```json
{
  "input_kind": "google_maps_url",
  "original_input": "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022",
  "normalized_input": "16.054400, 108.202200",
  "latitude": 16.0544,
  "longitude": 108.2022,
  "source_url": "https://www.google.com/maps/search/?api=1&query=16.0544%2C108.2022",
  "resolution_method": "query_query"
}
```

- `input_kind`: the broad input category.
- `normalized_input`: coordinates normalized to six decimal places.
- `latitude` and `longitude`: numeric resolved coordinates.
- `source_url`: the full URL used for extraction; for a shared link, this is the redirected URL.
- `resolution_method`: the exact URL field or parser path that supplied the coordinates.

A successful run exits with status `0`. A parser or resolution error is printed as JSON to standard
error and exits with status `2`.

## 8. Current restrictions and common errors

### `LOCATION_NOT_SUPPORTED`

The current export only accepts coordinates inside its existing Da Nang region:

- Latitude: `15.9` through `16.3`
- Longitude: `107.8` through `108.4`

Valid coordinates outside this box are intentionally rejected.

### `UNSUPPORTED_LOCATION_INPUT`

Check that:

- The full URL uses `google.com` or a `google.com` subdomain.
- A shared link uses HTTPS and one of the supported hosts: `maps.app.goo.gl`, `goo.gl/maps`, or
  `g.page`.
- The input is not currently only a venue name, street address, Place ID, or CID.

### `MALFORMED_LOCATION_INPUT`

The input resembles a supported format, but no valid coordinate pair could be extracted. A Google
place page containing only an opaque identifier can produce this error.

### `SHORT_URL_RESOLUTION_FAILED` or `SHORT_URL_RESOLUTION_BLOCKED`

Retry once after checking the internet connection. If Google continues to block or time out, open
the link in Google Maps and copy the full URL, or copy the displayed coordinates directly.

## 9. Not implemented yet

The following inputs are placeholders for a future version and are not currently resolved:

- Venue-name text
- Address text
- Google Place ID
- Google CID

Future implementation requires Google Geocoding/Places API selection, protected API credentials,
candidate-selection rules, confidence metadata, caching and cost controls, provider-quota handling,
region validation, and additional tests. The current script does not make these API calls or require
an API key.

## Accuracy reminder

The script extracts coordinates encoded in a URL; it does not independently verify the physical
entrance, rooftop, parcel boundary, address, or venue. Google URL formats and redirect behavior can
also change. Confirm coordinates against an authoritative map or onsite source when accuracy is
important.
