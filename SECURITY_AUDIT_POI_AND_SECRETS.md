# Security Audit: Secrets/DB URL Exposure + POI Access Path

Date: 2026-04-22

## 1) MasterList status (obsolete) and all remaining references
`static/masterlist.json` is obsolete and has been removed from the public static directory.

Current references discovered in code/docs:
- `DEVELOPER_GUIDE.md` previously documented `MasterList.json` in the tree (now removed/updated).
- `check_dist.py` had a comment reference (`# MasterList`) (now updated).
- `SECURITY_AUDIT_POI_AND_SECRETS.md` historical mention of removal.

There is no runtime code path that loads `static/masterlist.json`.

## 2) Construction scoring location and required POI distance tiers
Construction score is computed in `SearchService.run(...)`.

The construction path now calls a secure POI query method:
- `POIService.get_construction_distance_bins(lat, lon)`
- Distance bins: `[0,10)`, `[10,20)`, `[20,30)`, `[30,40)` meters
- Weighted score:
  - `0-10m`: weight 4
  - `10-20m`: weight 3
  - `20-30m`: weight 2
  - `30-40m`: weight 1
  - final: `min(100, weighted * 10)`

## 3) Bounding-box usage
A bounding-box check is now enforced in `/api/search` via `is_inside_app_bbox(lat, lon)` using configurable `APP_BOUNDING_BOX`.
Out-of-bounds requests are rejected with `422 OUT_OF_BOUNDS` before compute.

Additionally, POI SQL retrieval uses a bounding-box prefilter (`ST_MakeEnvelope`) before exact `ST_DWithin`.

## 4) Where raw `pois` table is queried (entire repo)
Raw `pois` reads/writes now found at:
- `app/services/poi_service.py`:
  - runtime secure read query for distance bins against `pois` (parameterized SQL)
- `jobs/daily_precompute.py`:
  - offline precompute job query `SELECT ... FROM pois` to populate `cell_poi_precompute`
- `add_poi.py`:
  - admin/utility insert `INSERT INTO pois ...`

Security posture:
- Runtime query in `POIService` uses bound parameters (`:lat`, `:lon`, bounds), no string interpolation.
- Batch precompute query has no user input (fixed SQL text in scheduled job).
- Utility insert script uses bound parameters for values.

## 5) Dynamic API leak audit (routes/models/serializers)
Audit covered API handlers in `app/api/*.py`, response models in `app/schemas/*.py` and `app/models/dto.py`.

Result:
- No route returns raw `settings`, `Config`, SQLAlchemy engine/session objects, or database connection URLs in JSON responses.
- Response models are narrow and explicit (`SearchResponse`, `StatusResponse`, `UnlockIntentResponse`, etc.).
- Webhook dependency helper `get_services()` provides internal dependencies only to server-side route logic; these are not serialized to clients.

## 6) Redis log leak fix
`app/main.py` startup debug prints that exposed a prefix of `UPSTASH_REDIS_REST_URL` were removed.
Only boolean presence flags remain in structured debug logs.

## Conclusion
- Obsolete masterlist runtime surface: removed.
- Construction score now uses direct secured `pois`-distance bins in required meter tiers.
- Bounding-box guardrails are now applied at API ingress and POI SQL prefilter.
- Raw `pois` access points across app/jobs/scripts were identified and reviewed for safety.
- Startup Redis URL prefix leak fixed.
