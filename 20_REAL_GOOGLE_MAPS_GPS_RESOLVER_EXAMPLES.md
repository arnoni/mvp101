# 20 real Google Maps GPS resolver examples

These examples exercise every input and extraction path currently present in
`standalone_gps_resolver.py`. All coordinates are inside the resolver's current Da Nang bounding
box.

The response examples show the stable, relevant JSON fields. The actual script also returns
`original_input` and `source_url`. Shared-link runs can print diagnostic log lines before the JSON,
and their redirected `source_url` can acquire new Google tracking parameters over time.

Verification date: **2026-08-05**.

## Important `g.page` limitation

Examples 1–18 and 20 produce valid coordinate responses. Example 19 covers the accepted `g.page`
input path, but the tested real link currently redirects to a Google place-ID-only URL without
coordinates. The present resolver therefore returns its documented structured error. A fabricated
successful response is deliberately not shown.

## Ground-truth venues used

- **Crowne Plaza Danang by IHG** — `16.0273539, 108.2565294`; 08 Vo Nguyen Giap, Khue My ward,
  Da Nang. The address and shared link are published in an
  [ICAO meeting bulletin](https://www.icao.int/sites/default/files/APAC/Meetings/2026/2026%20AAC-6/General%20Information/Attachment-B-Meeting-Bulletin.pdf).
- **Maison Spa** — `16.0675937, 108.244808`; 216 Vo Nguyen Giap, Phuoc My, Son Tra, Da Nang. The
  business page publishes the [legacy Google Maps link](https://pubhtml5.com/homepage/mppk/).
- **Voi's Kitchen, 108 Thu Khoa Huan** — `16.055445, 108.2416348`.
- **Voi's Kitchen, 12 Duong Tu Minh** — `16.0672054, 108.2405886`. Both addresses and Google Maps
  links are published on [Voi's Kitchen's website](https://www.voiskitchen.com/).
- **Tashi Ocean Garden Hotel & Apartment Da Nang** — shared-link pin `16.0776236, 108.2426482`.
  The full browser URL in example 20 contains an earlier place-data pair at
  `16.0777299, 108.242299`, which the current parser selects by design.
- **Hanami Hotel Danang** — 61–63 Hoang Ke Viem, Ngu Hanh Son, Da Nang. The address and `g.page`
  link are published on the [hotel's official website](https://hanamihotel.com/).

---

## 1. Raw decimal latitude, longitude

1. **Input:** `16.0273539, 108.2565294`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py '16.0273539, 108.2565294'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"decimal_pair","normalized_input":"16.027354, 108.256529","latitude":16.0273539,"longitude":108.2565294,"resolution_method":"decimal_pair"}
   ```

4. **Ground truth:** Crowne Plaza Danang by IHG — `16.0273539, 108.2565294`.

## 2. Raw space-separated latitude and longitude

1. **Input:** `16.0675937 108.244808`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py '16.0675937 108.244808'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"decimal_pair","normalized_input":"16.067594, 108.244808","latitude":16.0675937,"longitude":108.244808,"resolution_method":"decimal_pair"}
   ```

4. **Ground truth:** Maison Spa — `16.0675937, 108.244808`.

## 3. Raw reversed longitude, latitude

1. **Input:** `108.2416348, 16.055445`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py '108.2416348, 16.055445'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"decimal_pair","normalized_input":"16.055445, 108.241635","latitude":16.055445,"longitude":108.2416348,"resolution_method":"decimal_pair_reversed"}
   ```

4. **Ground truth:** Voi's Kitchen, 108 Thu Khoa Huan — `16.055445, 108.2416348`.

## 4. Raw DMS coordinates

1. **Input:** `16°03:19.602N 108°14:29.88528E`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py '16°03:19.602N 108°14:29.88528E'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"degree_pair","normalized_input":"16.055445, 108.241635","latitude":16.055445000000002,"longitude":108.2416348,"resolution_method":"degree_pair"}
   ```

4. **Ground truth:** Voi's Kitchen, 108 Thu Khoa Huan — decimal equivalent
   `16.055445, 108.2416348`.

## 5. Full Google Maps URL using `q`

1. **Input:** `https://www.google.com/maps?q=16.0273539%2C108.2565294`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps?q=16.0273539%2C108.2565294'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.027354, 108.256529","latitude":16.0273539,"longitude":108.2565294,"resolution_method":"query_q"}
   ```

4. **Ground truth:** Crowne Plaza Danang by IHG — `16.0273539, 108.2565294`.

## 6. Full Google Maps URL using `ll`

1. **Input:** `https://maps.google.com/maps?ll=16.0675937%2C108.244808`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://maps.google.com/maps?ll=16.0675937%2C108.244808'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.067594, 108.244808","latitude":16.0675937,"longitude":108.244808,"resolution_method":"query_ll"}
   ```

4. **Ground truth:** Maison Spa — `16.0675937, 108.244808`.

## 7. Google Maps search URL using `query`

1. **Input:** `https://www.google.com/maps/search/?api=1&query=16.055445%2C108.2416348`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/search/?api=1&query=16.055445%2C108.2416348'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.055445, 108.241635","latitude":16.055445,"longitude":108.2416348,"resolution_method":"query_query"}
   ```

4. **Ground truth:** Voi's Kitchen, 108 Thu Khoa Huan — `16.055445, 108.2416348`.

## 8. Full Google Maps URL using `center`

1. **Input:** `https://www.google.com/maps?center=16.0672054%2C108.2405886`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps?center=16.0672054%2C108.2405886'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.067205, 108.240589","latitude":16.0672054,"longitude":108.2405886,"resolution_method":"query_center"}
   ```

4. **Ground truth:** Voi's Kitchen, 12 Duong Tu Minh — `16.0672054, 108.2405886`.

## 9. Directions URL using `destination`

1. **Input:** `https://www.google.com/maps/dir/?api=1&destination=16.0776236%2C108.2426482`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/dir/?api=1&destination=16.0776236%2C108.2426482'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.077624, 108.242648","latitude":16.0776236,"longitude":108.2426482,"resolution_method":"query_destination"}
   ```

4. **Ground truth:** Tashi Ocean Garden Hotel & Apartment Da Nang —
   `16.0776236, 108.2426482`.

## 10. Directions URL using `origin`

1. **Input:** `https://www.google.com/maps/dir/?api=1&origin=16.0273539%2C108.2565294`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/dir/?api=1&origin=16.0273539%2C108.2565294'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.027354, 108.256529","latitude":16.0273539,"longitude":108.2565294,"resolution_method":"query_origin"}
   ```

4. **Ground truth:** Crowne Plaza Danang by IHG — `16.0273539, 108.2565294`.

## 11. Legacy directions URL using `saddr`

1. **Input:** `https://www.google.com/maps/dir/?saddr=16.0675937%2C108.244808`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/dir/?saddr=16.0675937%2C108.244808'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.067594, 108.244808","latitude":16.0675937,"longitude":108.244808,"resolution_method":"query_saddr"}
   ```

4. **Ground truth:** Maison Spa — `16.0675937, 108.244808`.

## 12. Legacy directions URL using `daddr`

1. **Input:** `https://www.google.com/maps/dir/?daddr=16.055445%2C108.2416348`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/dir/?daddr=16.055445%2C108.2416348'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.055445, 108.241635","latitude":16.055445,"longitude":108.2416348,"resolution_method":"query_daddr"}
   ```

4. **Ground truth:** Voi's Kitchen, 108 Thu Khoa Huan — `16.055445, 108.2416348`.

## 13. Place-data URL using `!3d...!4d...`

1. **Input:** `https://www.google.com/maps/place/Voi%27s+Kitchen/@16.060000,108.230000,17z/data=!3d16.0672054!4d108.2405886`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/place/Voi%27s+Kitchen/@16.060000,108.230000,17z/data=!3d16.0672054!4d108.2405886'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.067205, 108.240589","latitude":16.0672054,"longitude":108.2405886,"resolution_method":"place_3d4d"}
   ```

4. **Ground truth:** Voi's Kitchen, 12 Duong Tu Minh — `16.0672054, 108.2405886`. This also
   confirms place-data coordinates take priority over the different viewport pair.

## 14. Reverse place-data URL using `!2d...!3d...`

1. **Input:** `https://www.google.com/maps/place/Tashi/data=!2d108.2426482!3d16.0776236`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/place/Tashi/data=!2d108.2426482!3d16.0776236'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.077624, 108.242648","latitude":16.0776236,"longitude":108.2426482,"resolution_method":"place_2d3d"}
   ```

4. **Ground truth:** Tashi Ocean Garden Hotel & Apartment Da Nang —
   `16.0776236, 108.2426482`.

## 15. Viewport-only URL using `@lat,lng`

1. **Input:** `https://www.google.com/maps/place/Crowne+Plaza+Danang/@16.0273539,108.2565294,17z`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/place/Crowne+Plaza+Danang/@16.0273539,108.2565294,17z'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.027354, 108.256529","latitude":16.0273539,"longitude":108.2565294,"resolution_method":"viewport_center"}
   ```

4. **Ground truth:** The viewport is intentionally centered on Crowne Plaza Danang's verified pin,
   `16.0273539, 108.2565294`. In arbitrary Maps URLs, viewport centers are not guaranteed to be
   exact venue pins.

## 16. URL containing encoded DMS coordinates

1. **Input:** `https://www.google.com/maps/place/16%C2%B004%2739.44496%22N+108%C2%B014%2733.53352%22E`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/place/16%C2%B004%2739.44496%22N+108%C2%B014%2733.53352%22E'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.077624, 108.242648","latitude":16.0776236,"longitude":108.2426482,"resolution_method":"decoded_dms"}
   ```

4. **Ground truth:** The encoded DMS values convert exactly to Tashi Ocean Garden Hotel &
   Apartment's shared-link pin, `16.0776236, 108.2426482`.

## 17. Live `maps.app.goo.gl` shared link

1. **Input:** `https://maps.app.goo.gl/2gfRJfKFBPF4paFQ8`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://maps.app.goo.gl/2gfRJfKFBPF4paFQ8'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_short_url","normalized_input":"16.027354, 108.256529","latitude":16.0273539,"longitude":108.2565294,"resolution_method":"place_3d4d"}
   ```

4. **Ground truth:** Crowne Plaza Danang by IHG — `16.0273539, 108.2565294`; shared link and
   address published by ICAO.

## 18. Live legacy `goo.gl/maps` shared link

1. **Input:** `https://goo.gl/maps/j2khUCjm1dAD69Px7`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://goo.gl/maps/j2khUCjm1dAD69Px7'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_short_url","normalized_input":"16.067594, 108.244808","latitude":16.0675937,"longitude":108.244808,"resolution_method":"place_3d4d"}
   ```

4. **Ground truth:** Maison Spa — `16.0675937, 108.244808`; the published address is 216 Vo
   Nguyen Giap, Phuoc My, Son Tra, Da Nang.

## 19. Real `g.page` link — recognized path, current coordinate-free result

1. **Input:** `https://g.page/hanamihotel`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://g.page/hanamihotel'
   ```

3. **Expected current response (not a valid coordinate response):**

   ```json
   {"error_code":"MALFORMED_LOCATION_INPUT","message":"This Google Maps link points to a place page that does not expose coordinates. Please paste the full Google Maps URL or type the coordinates directly."}
   ```

4. **Ground truth:** Hanami Hotel Danang, 61–63 Hoang Ke Viem, Ngu Hanh Son, Da Nang. Its official
   site publishes this `g.page` URL. Google currently redirects it to a place-ID-only URL, which
   proves venue identity but does not expose coordinates to the present parser.

## 20. Real full browser-generated Google Maps URL

1. **Input:**
   `https://www.google.com/maps/place/Tashi+Ocean+Garden+Hotel+%26+Apartment+Da+Nang/@16.077462,108.2419443,19z/data=!4m20!1m8!3m7!1s0x314217893f8ae817:0xb2eb103f179a78ed!2zMjEgUGjGsOG7m2MgVHLGsOG7nW5nIDExLCBBbiBI4bqjaSwgxJDDoCBO4bq1bmcgNTUwMDAwLCBWaWV0bmFt!3b1!8m2!3d16.0777299!4d108.242299!16s%2Fg%2F11jyly4fyb!3m10!1s0x31421714368a092b:0x318717f306c12aec!5m4!1s2026-05-23!2i3!4m1!1i2!8m2!3d16.0776236!4d108.2426482!16s%2Fg%2F11kbp5srhq!18m1!1e1?entry=ttu&g_ep=EgoyMDI2MDUxMy4wIKXMDSoASAFQAw%3D%3D`
2. **PowerShell:**

   ```powershell
   uv run standalone_gps_resolver.py 'https://www.google.com/maps/place/Tashi+Ocean+Garden+Hotel+%26+Apartment+Da+Nang/@16.077462,108.2419443,19z/data=!4m20!1m8!3m7!1s0x314217893f8ae817:0xb2eb103f179a78ed!2zMjEgUGjGsOG7m2MgVHLGsOG7nW5nIDExLCBBbiBI4bqjaSwgxJDDoCBO4bq1bmcgNTUwMDAwLCBWaWV0bmFt!3b1!8m2!3d16.0777299!4d108.242299!16s%2Fg%2F11jyly4fyb!3m10!1s0x31421714368a092b:0x318717f306c12aec!5m4!1s2026-05-23!2i3!4m1!1i2!8m2!3d16.0776236!4d108.2426482!16s%2Fg%2F11kbp5srhq!18m1!1e1?entry=ttu&g_ep=EgoyMDI2MDUxMy4wIKXMDSoASAFQAw%3D%3D'
   ```

3. **Expected valid response:**

   ```json
   {"input_kind":"google_maps_url","normalized_input":"16.077730, 108.242299","latitude":16.0777299,"longitude":108.242299,"resolution_method":"place_3d4d"}
   ```

4. **Ground truth:** This real URL contains two `!3d...!4d...` pairs. The current parser returns the
   first pair, `16.0777299, 108.242299`, exactly as covered by its regression test. The later pair,
   `16.0776236, 108.2426482`, is the Tashi shared-link venue pin. This example documents why long
   browser URLs can contain more than one plausible coordinate pair.

---

## Coverage summary

| Parser path | Examples |
|---|---:|
| Decimal pair, comma | 1 |
| Decimal pair, whitespace | 2 |
| Reversed decimal pair | 3 |
| Raw DMS pair | 4 |
| Query keys `q`, `ll`, `query`, `center`, `destination`, `origin`, `saddr`, `daddr` | 5–12 |
| Place data `!3d...!4d...` | 13, 17, 18, 20 |
| Reverse place data `!2d...!3d...` | 14 |
| Viewport `@lat,lng` | 15 |
| Encoded DMS URL | 16 |
| `maps.app.goo.gl` | 17 |
| `goo.gl/maps` | 18 |
| `g.page` recognized-but-coordinate-free path | 19 |
| Full browser-generated Google Maps URL | 20 |

## Accuracy and maintenance note

The synthetic full URLs above use coordinates returned by verified live Google links, so their
expected parser outputs are deterministic. They do not prove rooftop, parcel, entrance, or legal
boundary accuracy. Live short-link redirects and Google URL formats can change after the
verification date; rerun the examples when publishing a new resolver version.
