# DillDrill — URL-First Radar MVP

**DillDrill** is a privacy-focused tool for detecting construction noise and real-estate points of interest (POIs). It allows users to check specific coordinates for nearby projects using a tiered access system.

## Features (v1.1)
- **Direct Coordinate Input**: No address geocoding; fast and privacy-preserving.
- **Privacy First**: Anonymous IDs are random UUIDs stored in signed cookies (HMAC); no account required.
- **Privacy Logging**: Logs use coarse area codes only; no precise coordinates.
- **Smart Filtering**: "Greedy 30m" algorithm ensures result diversity.
- **Tiered Access**:
  - **Free Tier**: 2 searches/day, 1 result per search.
  - **Paid Tier**: 50 searches/day, 5 results per search.
- **Cloudflare Turnstile**: Human verification required for Free tier; validated server-side.
- **Language Persistence**: Stores `dd_lang` and uses it in anonymous fingerprinting; defaults to last choice.
- **KMZ Export**: Download results for Google Earth.
- **Internationalization**: English, Spanish, Russian, Korean.
- **Translation Cache**: Server translations are kept in a process LRU cache with optional warmup at import time.
- **Dev Mode Visibility**: Landing page indicates when Redis fallback (in-memory quota) is active.
- **Preflight Status**: `/api/status` endpoint powers instant gating (can_search, turnstile_required).
- **SSR Hydration**: Initial status and tier are pre-rendered on the server.
- **Accessibility**: "How to use" icon with `aria-label`; Message Board uses `role="status"`.
- **Sentry Observability**: Integrated error reporting and performance monitoring via `sentry-sdk` with FastAPI/Starlette integrations.
- **PostGIS-backed Search**: Uses Neon PostgreSQL with PostGIS and SQLAlchemy 2 async engine (NullPool + pre_ping).
- **Strict DTOs**: Public responses use meters (`distance_m`) and typed URLs; extra fields are forbidden.
- **Fail-Closed Security**: Quota enforcement fails closed if Redis is unavailable (no in-memory fallback in production).
- **Hybrid Entitlement**: Redis acts as a hot cache for user tier/status, falling back to the PostgreSQL `subscriptions` table on miss.
- **Idempotent Webhooks**: Billing events are deduplicated via `webhook_events` before processing.

## Architecture
The project follows a Domain-First architecture using FastAPI, Redis (Upstash), and PostGIS (Neon PostgreSQL) for POIs, with server-rendered Jinja2. Database access is standardized via SQLAlchemy 2 async engine with `NullPool` and `pool_pre_ping`. Quota enforcement is strict and backed by Redis.

### Security & Entitlement
- **Session vs. Entitlement**: `SessionMiddleware` hydrates `user_id` and `csrf` only. `EntitlementMiddleware` computes `tier` using `EntitlementService` and sets `entitlement_stale`; it does not trust any session payload tier.
- **Cache Blob Versioning**: Redis `entitlement:user:{user_id}` stores `schema_version`, `tier`, `verified_at`, `provider`, `subscription_status`, `plan`, `period_end`.
- **Monotonic Staleness**: Treat as stale when `now - verified_at > ttl_seconds`, or if `verified_at` is missing/in the future (clock skew). Corrupt JSON self-heals by deleting the key.
- **Error Mapping**:
  - `401` — Missing/invalid `dd_session` (no `user_id` in session payload)
  - `403` — Logged-in user but `tier == FREE` on paid-required routes
  - `503` — Entitlement cannot be verified (stale/miss/Redis down) → returns `ENTITLEMENT_UNVERIFIED`
- **Quota Identity Rule**: If `tier == PAID` and `entitlement_stale == False` and `user_id` exists, use user-scoped keys. Otherwise use anon-scoped keys.

## Developer Guide
For a detailed introduction to the codebase, modules, and architecture, please read the **[Developer Introduction & Architecture Guide](DEVELOPER_GUIDE.md)**.

## UGC Schema (Neon PostgreSQL)

```sql
-- Enum for moderation lifecycle
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ugc_report_status') THEN
    CREATE TYPE ugc_report_status AS ENUM (
      'pending',
      'approved',
      'rejected',
      'hidden',
      'duplicate'
    );
  END IF;
END
$$;

-- Main UGC reports table
CREATE TABLE ugc_reports (
  id BIGSERIAL PRIMARY KEY,

  -- reporter identity (server generated)
  reporter_anon_id TEXT NOT NULL,
  reporter_user_id BIGINT NULL,
  reporter_tier TEXT NOT NULL DEFAULT 'free',

  -- user submitted content
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  category TEXT NULL CHECK (char_length(category) <= 50),
  noise_type TEXT NULL CHECK (char_length(noise_type) <= 50),
  severity SMALLINT NULL CHECK (severity BETWEEN 1 AND 5),
  occurred_at TIMESTAMPTZ NULL,

  -- spatial data
  geom GEOGRAPHY(POINT, 4326) NOT NULL,

  -- moderation lifecycle
  status ugc_report_status NOT NULL DEFAULT 'pending',
  moderator_note TEXT NULL,
  moderated_at TIMESTAMPTZ NULL,

  -- dedup + clustering helpers (server generated)
  content_hash TEXT NOT NULL,
  geo_cell TEXT NOT NULL,
  duplicate_of_id BIGINT NULL REFERENCES ugc_reports(id),

  -- optional POI linkage
  nearest_poi_id BIGINT NULL REFERENCES pois(id),
  nearest_poi_distance_m INTEGER NULL,

  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Evidence URLs normalized
CREATE TABLE ugc_report_evidence (
  id BIGSERIAL PRIMARY KEY,
  report_id BIGINT NOT NULL REFERENCES ugc_reports(id) ON DELETE CASCADE,
  url TEXT NOT NULL CHECK (char_length(url) <= 2000),
  url_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------
-- INDEXES
-- --------------------

-- spatial queries (nearby reports)
CREATE INDEX ugc_reports_geom_gist
ON ugc_reports USING GIST (geom);

-- duplicate detection helpers
CREATE INDEX ugc_reports_geo_cell_created_at
ON ugc_reports (geo_cell, created_at DESC);

CREATE INDEX ugc_reports_content_hash
ON ugc_reports (content_hash);

CREATE INDEX ugc_reports_reporter_anon_created_at
ON ugc_reports (reporter_anon_id, created_at DESC);

-- feed and moderation queries
CREATE INDEX ugc_reports_status_created_at
ON ugc_reports (status, created_at DESC);

CREATE INDEX ugc_reports_status_occurred_at
ON ugc_reports (status, occurred_at DESC);

-- evidence lookup
CREATE INDEX ugc_report_evidence_report_id
ON ugc_report_evidence (report_id);

CREATE UNIQUE INDEX ugc_report_evidence_unique_per_report
ON ugc_report_evidence (report_id, url_hash);

-- --------------------
-- updated_at auto-maintenance
-- --------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ugc_reports_set_updated_at
BEFORE UPDATE ON ugc_reports
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
```

### UGC Public ID Migration

```sql
ALTER TABLE ugc_reports 
  ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid();

CREATE UNIQUE INDEX IF NOT EXISTS ugc_reports_public_id_uq 
  ON ugc_reports (public_id);
```

### UGC Submit (Plan v2)
- Endpoint: POST `/api/ugc/report-submit`
- Turnstile: Mandatory for everyone; admin bypass disabled for UGC.
- CSRF: Skipped by design; rely on Turnstile + strict limits + gate rate limiting.
- Quota: Consumed via `run_gate` before any persistence.
- DTO: `title`, `description`, `lat`, `lon`, `category?`, `severity?`, `evidence_urls?` (≤5, each ≤500 chars), `turnstile_token` (required).
- Validation: Bbox check; severity must be 1–5; evidence list constraints as above.
- Dedup: 7-day TTL Redis key `ugc:dedup:{sha256(anon_id|geo_cell|content_hash|YYYYMMDD)}`; geo_cell quantized at ~50m via rounding to 3 decimals; content_hash is sha256 of normalized title|description|category.
- Dedup: 7-day TTL Redis key `ugc:dedup:{sha256(anon_id|geo_cell|content_hash|YYYYMMDD)}`; geo_cell quantized on a 0.0005° grid (~50–55 m), coordinates snapped to nearest step; content_hash is sha256 of normalized title|description|category.
- Postgres: Insert into `ugc_reports` with `public_id`, identity, content, `geom = ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography`, `status='pending'`, `content_hash`, `geo_cell`. Defaults handle timestamps.
- Evidence: Store list in Redis at `ugc:evidence:{public_id}` (JSON; TTL 7 days).
- Response: `{ ok: true, duplicate: boolean, report_id: public_id }`.

### UGC Validity Notes
- Short answer: yes, the table is valid and very solid. Nothing is wrong here; the schema is appropriate for MVP and scales well with PostGIS.
- "index already exists" messages are expected if a migration is re-run using plain CREATE INDEX. Use CREATE INDEX IF NOT EXISTS for idempotent migrations to avoid errors, as shown above.
- The enum creation block uses a safe DO/IF NOT EXISTS pattern, which prevents duplicate type creation on repeated runs.
- Validation checklist:
  - Primary keys on ugc_reports and ugc_report_evidence
  - GEOGRAPHY(POINT, 4326) with GIST index for spatial queries
  - Dedup helpers: geo_cell, content_hash, and evidence unique per report (report_id, url_hash)
  - Moderation lifecycle via ugc_report_status enum and status indexes
  - updated_at trigger function and trigger on ugc_reports
  - public_id column with unique index for external references
  - Reasonable TEXT length checks on category/noise_type and evidence URL

## Getting Started

1.  **Clone the repo**.
2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
    On Windows, building `asyncpg` may require Microsoft C++ Build Tools. Alternatively, use WSL/Linux or Python 3.11/3.12 environments where prebuilt wheels are available.
3.  **Configure Environment**:
    Create a `.env` file:
    ```env
    DATABASE_URL="postgresql://user:pass@host/db?sslmode=require"
    ENABLE_REDIS="true"
    REDIS_URL="redis://..."
    SECRET_KEY="your_secret_key_here"
    CLOUDFLARE_TURNSTILE_SECRET=""
    CLOUDFLARE_TURNSTILE_SITE_KEY=""
    ENV="development" # or "production"
    APP_ORIGIN="http://localhost:8000"
    # i18n cache controls
    I18N_LRU_MAX="8"
    I18N_WARMUP="true"
    # Optional: enable admin bypass with a static token
    ADMIN_BYPASS_TOKEN="..."
    ```
4.  **Run**:
    ```bash
    uvicorn app.main:app --reload
    ```
    Health check:
    - `GET /health` → {"status":"ok"}
    - `GET /health/db` → {"db":"ok"} when `DATABASE_URL` is configured and reachable

### UI Contract v1 Highlights
- Backend returns user_status, can_search, turnstile_required, checks_today, tier, results_state.
- Frontend uses enums for i18n; server text acts only as a fallback.
- Status Strip is clickable and opens Support modal; Message Board shows narratives; Results Board remains visual.
- Status refresh queries `/api/status` on load and window focus with debounce.
- Results items (public): name, distance_m (meters), google_maps_link (HttpUrl), optional image_url (HttpUrl). Coordinates are available via a separate variant for KMZ generation.

### Notes
- KMZ quota is aligned to the daily key pattern (`daily_read:{YYYYMMDD}:{anon_id}`).
- Dev Mode shows Redis fallback and Turnstile indicators.

### Developer Tooling
- Linting: Ruff configured via pyproject (rules: E, F, I, UP, B). Run:
  ```bash
  ruff .
  ```
- Import guard: Fail if `sqlalchemy.orm.Session` is imported outside migrations or admin scripts. Run:
  ```bash
  python check_no_session_import.py
  ```
