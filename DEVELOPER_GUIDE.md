# Developer Introduction & Architecture Guide

Welcome to the **DillDrill** codebase! This guide is designed to help new developers understand the project structure, the Domain-First architecture, and the specific requirements of Technical Design Document (TDD) v1.1.

## 1. Project Overview

**DillDrill** is a "URL-First Radar" for detecting construction noise and real-estate points of interest (POIs).

**Key Philosophy (TDD v1.1):**
*   **Privacy-First:** We track generic "Anonymous IDs" rather than user accounts.
*   **Domain-Driven:** Core logic resides in services, not in the API routes.
*   **Lat/Lng Native:** Users (or the frontend) provide raw coordinates. We do *not* geocode server-side (Mapbox integration has been removed).
*   **Tiered Access:** A "Policy Engine" governs who can see what and how often.

## 2. High-Level Architecture

The application follows a **Service-Layer Pattern** built on FastAPI.

```mermaid
graph TD
    Client[Client / Frontend] -->|HTTP Request| Middleware[AnonIdMiddleware]
    Middleware -->|Standardized Request| Route[API Routes]
    
    Route -->|Context| PolicyEngine[Policy Engine]
    
    subgraph "Domain Core"
        PolicyEngine -->|Check| QuotaRepo[Quota Repository (Redis)]
        PolicyEngine -->|Check| Entitlement[Entitlement Service]
        
        Route -->|If Allowed| POIService[POI Service]
        POIService -->|Read| PostGIS[Neon PostgreSQL]
    end
    
    POIService -->|Results| Route
    Route -->|Response| Client
```

## 3. Module Guide

### 3.1 Core (`app/core/`)
*   **`config.py`**: Centralized configuration using Pydantic `BaseSettings`. Handles environment variables, feature flags (e.g., `ENABLE_REDIS`), constants like `DA_NANG_BBOX`, and search defaults:
    *   `SEARCH_RADIUS_KM` (default 0.8)
    *   `MIN_SPACING_M` (default 30)
    *   `MAX_RESULTS_DEFAULT` (default 5)
    *   `SECRET_KEY` (required for cookie signing)
    *   `REDIS_URL` (connection string)
*   **`middleware.py`**:
    *   **`AnonIdMiddleware`**: Ensures each request carries `dd_anon_id`. If missing or invalidly signed, mints a new random UUID and sets a signed cookie (HMAC-SHA256). Attributes: `HttpOnly`, `Secure` (prod), `SameSite=Lax`.
    *   **`SessionMiddleware`**: Hydrates `user_id` and `csrf` from Redis `session:{sid}` based on `dd_session` cookie. Does not decide paid tier.
    *   **`EntitlementMiddleware`**: Computes `tier` via `EntitlementService` and sets `request.state.tier` and `request.state.entitlement_stale`. It does not trust any tier in the session payload.

### 3.2 Services (`app/services/`)
This is where the business logic lives.

*   **`policy_engine.py` (The Brain):**
    *   **Responsibility:** Decides *if* a request can proceed.
    *   **Logic:** Checks User Tier (Free vs. Paid) -> Checks Quota -> Checks for required Friction (Turnstile).
    *   **Output:** Returns a `PolicyDecision` (ALLOW, BLOCK, CHALLENGE_REQUIRED).
    *   **Key Concept:** It does *not* execute the search; it only guards the door.

*   **`poi_service.py` (The Search):**
    *   **Responsibility:** Finds relevant data.
    *   **Algorithm (Greedy spacing):**
        1.  Query PostGIS for POIs within `SEARCH_RADIUS_KM` using `ST_DWithin`.
        2.  Order by `ST_Distance` ascending.
        3.  Apply spacing via `MIN_SPACING_M` client-side (Greedy) to ensure diversity.
    *   **Data Source:** Neon PostgreSQL with PostGIS (`pois` table).
    *   **Implementation notes:** SQLAlchemy 2 async engine; typed Postgres array bind with `ARRAY(Text)` for `WHERE name = ANY(:names)`.

*   **`quota_repository.py` (The State):**
    *   **Responsibility:** specific usage tracking.
    *   **Implementation:** Primary backing is **Redis**. If Redis fails in production, it fails closed (raises error).
    *   **Async:** Fully async operation.

*   **`area_bucketer.py`:**
    *   **Responsibility:** Privacy and caching logic. Converts a precise float Lat/Lng into a coarse "Area Code" string. Used for aggregating usage stats without tracking precise user locations. (Currently stubbed to 3 decimal places).

*   **`entitlement_service.py`:**
    *   **Responsibility:** Determines if a user is `tier: FREE` or `tier: PAID`.
    *   **Logic:**
        1.  **Cache Check:** Reads `entitlement:user:{user_id}` from Redis. Payload includes `schema_version`, `tier`, `verified_at`, `provider`, `subscription_status`, `plan`, `period_end`.
        2.  **Monotonic Staleness:** Treat as stale when `now - verified_at > ttl_seconds`. If `verified_at` is missing or in the future (clock skew), treat as stale.
        3.  **Self-Healing:** If JSON parse fails, delete the corrupt key and treat as stale.
        4.  **Fallback:** On cache miss or Redis down, mark as stale (`FREE` tier until DB fallback is implemented).
    *   **Data Source:** Planned fallback to `subscriptions` (Postgres) on cache miss (TBD).

*   **`kmz_service.py`:**
    *   **Responsibility:** Generates Google Earth (`.kmz`) files dynamically from search results.

*   **`i18n.py`:**
    *   **Responsibility:** Simple in-memory translation service for the server-rendered frontend (supports EN, ES, RU, KO).

### 3.3 API (`app/api/`)
*   **`routes.py`**:
    *   **`/api/status`**: Preflight gating endpoint. Computes `user_status`, `can_search`, `turnstile_required`, `checks_today`, and `tier` without consuming quota. Respects admin bypass via `X-Admin-Auth` when `settings.ADMIN_BYPASS_TOKEN` is set. See [routes.py](file:///c:/Users/arnon/Documents/dev/projects/github/mine/trae_ide/mvp101/app/api/routes.py#L44-L108).
    *   **`/api/find-nearest`**: The main search endpoint. It accepts Lat/Lng, invokes the Policy Engine, and if allowed, calls the POI Service. Turnstile is required for Free tier requests when the token is missing.
    *   **`/download-kmz`**: Generates a file download based on the previous search and counts as a read. Quota key uses the daily scoped pattern `daily_read:{YYYYMMDD}:{anon_id}`. Uses coordinate-bearing DTOs for KMZ. See [routes.py](file:///c:/Users/arnon/Documents/dev/projects/github/mine/trae_ide/mvp101/app/api/routes.py#L329-L336).
    *   **Admin Bypass**: If `settings.ADMIN_BYPASS_TOKEN` is set, requests with header `X-Admin-Auth` equal to that token bypass quotas and Turnstile (does not overwrite quota keys). See [find_nearest](file:///c:/Users/arnon/Documents/dev/projects/github/mine/trae_ide/mvp101/app/api/routes.py#L137-L156).
    *   **Paid Dependencies**: Use `require_login` for session presence and `require_paid` for paid-only routes. `require_paid` enforces 403 (logged in but FREE), and 503 with `ENTITLEMENT_UNVERIFIED` when entitlement is stale or cannot be verified.

### 3.4 Utils (`app/utils/`)
*   **`security.py`**: Handles Cloudflare Turnstile verification.
*   **`haversine.py`**: Calculates distances between coordinates.

## 3.5 Frontend & SSR Hydration

The root endpoint pre-computes initial UI state and performs SSR hydration for the landing page:
*   Hydrates `initial_user_status`, `initial_can_search`, `initial_turnstile_required`, `initial_checks_today`, and `initial_tier` into the Jinja2 template.
*   Uses `dd_lang` cookie to select server-side translations.
*   Indicates whether Redis quota is using a fallback (in-memory) for developer visibility.
See [main.py root](file:///c:/Users/arnon/Documents/dev/projects/github/mine/trae_ide/mvp101/app/main.py#L147-L216).

## 4. TDD v1.1 Specification Highlights

If you are modifying the code, ensure you adhere to these strict rules from the TDD:

1.  **Input:** The API *must* accept `lat` and `lon` (float). Do not accept address strings (Geocoding removed).
2.  **Quota:** Every search consumes 1 unit of quota. The Policy Engine must strictly enforce:
    *   **Free Tier:** 2 reads / day.
    *   **Paid Tier:** 50 reads / day.
3.  **Friction:** We use Cloudflare Turnstile.
    *   If the Policy Engine returns `CHALLENGE_REQUIRED`, the client must present a valid `turnstile_token`.
4.  **Privacy:** Never log precise coordinates associated with a user ID. Use `AreaBucketer` if you need to aggregate spatial data.
5.  **Logging:** Use `structlog` for structured logging. Do not use standard `logging` directly for application logic.
6.  **DTOs:** Public results are strict (extra forbidden). Use meters (`distance_m`), `HttpUrl` types for links. Use a separate DTO variant when coordinates are required (e.g., KMZ).

## 4.3 Quota Identity Rule
*   Identity used for quota keys is governed strictly by entitlement:
    *   If `tier == PAID` and entitlement is fresh (`entitlement_stale == False`) and `user_id` exists, use `quota:user:{user_id}:{YYYYMMDD}`.
    *   Otherwise, use `quota:anon:{anon_id}:{YYYYMMDD}`.
*   This prevents stale paid sessions from consuming paid quota.

## 4.1 Internationalization & Accessibility
*   Enum-first i18n on the frontend; server text acts as a fallback only. Language preference `dd_lang` is persisted and folded into anonymous fingerprinting.
*   "How to use" is presented as an icon button with `aria-label`; Message Board uses `role="status"` to narrate outcomes for screen readers.

## 4.2 Status Refresh & Staleness
*   Initial status is hydrated on the server at page render.
*   Client re-fetches `/api/status` on load and on window focus with a debounce to avoid excessive polling.

## 6. Error Mapping
*   **401** — Missing or invalid `dd_session` (no `user_id` in session payload).
*   **403** — Logged-in user but `tier == FREE` for paid-required routes.
*   **503** — Entitlement cannot be verified (stale/cache miss/Redis down) for paid-required routes. Returns a stable error code: `ENTITLEMENT_UNVERIFIED`.

## 5. Getting Started

1.  **Environment Variables:** Ensure your `.env` file has (Redis/Turnstile optional):
    ```env
    DATABASE_URL="postgresql://user:pass@host/db?sslmode=require"
    ENABLE_REDIS="true"
    REDIS_URL="redis://..."
    SECRET_KEY="dev_insecure_secret"
    CLOUDFLARE_TURNSTILE_SECRET=""
    CLOUDFLARE_TURNSTILE_SITE_KEY=""
    ENV="development" # or "production"
    APP_ORIGIN="http://localhost:8000"
    ```
2.  **Run Locally:**
    ```bash
    uvicorn app.main:app --reload
    ```
    Health endpoints:
    *   `GET /health` → status only
    *   `GET /health/db` → requires `DATABASE_URL`; returns {"db":"ok"} when reachable
3.  **Testing Quotas:**
    *   The app uses cookies. To reset your identity/quota locally, delete the `dd_anon_id` cookie in your browser dev tools.

## 7. Database Schema

We use Neon PostgreSQL with PostGIS.

### Core Tables

#### `pois` (Spatial Data)
*   `name`: Text
*   `geom`: Geography(Point, 4326)
*   Spatial Index: GIST on `geom`

#### `users` (Identity)
*   `id`: UUID (PK)
*   `email`: Text (Unique)
*   `created_at`, `updated_at`: Timestamps

#### `subscriptions` (Billing)
*   `id`: UUID (PK)
*   `user_id`: UUID (FK -> users.id)
*   `provider`: Text (e.g., 'paddle', 'lemon_squeezy')
*   `provider_subscription_id`: Text (Unique)
*   `plan`: Text ('basic', 'pro')
*   `status`: Text ('active', 'trialing', 'past_due', 'canceled')
*   `current_period_end`: Timestamp

#### `webhook_events` (Idempotency)
*   `provider`: Text (PK Component)
*   `event_id`: Text (PK Component)
*   `payload`: JSONB
*   `status`: Text ('received', 'processed', 'failed')

### UGC Reports Schema (Neon SQL)

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

#### UGC Public ID Migration

```sql
ALTER TABLE ugc_reports 
  ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid();

CREATE UNIQUE INDEX IF NOT EXISTS ugc_reports_public_id_uq 
  ON ugc_reports (public_id);
```
## 8. Webhook Processing Flow

1.  **Ingest:** Webhook received -> Insert into `webhook_events` (provider, event_id).
    *   *Conflict?* -> Return 200 OK (Idempotent).
2.  **Process:** Parse payload -> Update `subscriptions` table.
    *   *Success?* -> Update `webhook_events.status` to 'processed'.
3.  **Reflect:** Next user request triggers Entitlement Service -> Cache Miss -> DB Read -> Redis Update.
├── app/
│   ├── api/            # Routes & endpoints
│   ├── core/           # Config, middleware
│   ├── models/         # Pydantic DTOs
│   ├── services/       # Domain logic (The most important folder)
│   ├── utils/          # Helpers (Haversine, security, etc.)
│   ├── main.py         # Entry point & lifespan management
│   └── logging.py      # Structured logging config
├── static/             # Assets (images, css, js) & MasterList.json
├── templates/          # Jinja2 HTML templates
└── requirements.txt    # Python dependencies
```
