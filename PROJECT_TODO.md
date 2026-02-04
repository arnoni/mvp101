# Project TODO

- [TBD] Implement signed admin bypass header for QA/dev superusers
  - Header: X-Admin-Auth
  - Use HMAC-signed token with expiry (e.g., 10–30 mins)
  - Token rotation policy and storage in Vercel env
  - Bypass ignores quotas and Turnstile, does not overwrite Redis
  - Restrict to specific endpoints and optional IP allowlist
  - Emit audit log event with anon_id and request_id
  - Disabled by default on production unless explicitly enabled

## Implementation Plan (TBD)
- Define token format and signing secret
- Middleware or dependency to validate header
- Add tests and observability (structured logs)

## Deferred Features
- Social proof strip with anonymized daily counts
- UGC reporting flow for POI corrections
- Support modal improvements and contribution telemetry

## Follow-ups
- [x] Update frontend to consume `distance_m` (meters) instead of `distance_km`.
- [x] Implement signed admin bypass header for QA/dev superusers (via `X-Admin-Auth` and config token)
- [x] Implement server-side session hydration (`SessionMiddleware` hydrates `user_id` and `csrf` from Redis).
- [ ] Finalize CSRF protection for mutation endpoints.
- [ ] Implement Entitlement Service DB fallback (subscriptions table). Redis cache includes `schema_version`, monotonic `verified_at` checks, and self-healing on corrupt JSON.
- [ ] Implement Webhook processing pipeline (`webhook_events` -> `subscriptions` update).
- [ ] Ensure KMZ flow uses coordinate-bearing DTOs consistently.
- [ ] Document Windows dev setup for `asyncpg` (MSVC Build Tools) or recommend WSL/Linux.
- [ ] Consider introducing a `paid_router` mounted with `dependencies=[require_paid]` for consistent application across paid-only endpoints.

### UGC vs Search Quota
- UGC burning search quota: Currently `run_gate` applies the same daily limit across endpoints, so UGC submissions reduce "find-nearest" capacity.
- If acceptable, keep as-is. If separation is desired without a large refactor, plan to support:
  - `run_gate(action="search"|"ugc")`
  - Gate chooses appropriate daily limit and whether Turnstile is always required for UGC.
  - Minimal route changes required.

### Neon SQL migration for UGC

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

-- public_id for external references
ALTER TABLE ugc_reports 
  ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid();

CREATE UNIQUE INDEX IF NOT EXISTS ugc_reports_public_id_uq 
  ON ugc_reports (public_id);
```
