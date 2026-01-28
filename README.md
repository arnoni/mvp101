# DillDrill — URL-First Radar MVP

**DillDrill** is a privacy-focused tool for detecting construction noise and real-estate points of interest (POIs). It allows users to check specific coordinates for nearby projects using a tiered access system.

## Features (v1.1)
- **Direct Coordinate Input**: No address geocoding; fast and privacy-preserving.
- **Privacy First**: Anonymous IDs via fingerprint stored in a cookie; no account required.
- **Privacy Logging**: Logs use coarse area codes only; no precise coordinates.
- **Smart Filtering**: "Greedy 30m" algorithm ensures result diversity.
- **Tiered Access**:
  - **Free Tier**: 2 searches/day, 1 result per search.
  - **Paid Tier**: 50 searches/day, 5 results per search.
- **Cloudflare Turnstile**: Human verification required for Free tier; validated server-side.
- **Language Persistence**: Stores `dd_lang` and uses it in anonymous fingerprinting; defaults to last choice.
- **KMZ Export**: Download results for Google Earth.
- **Internationalization**: English, Spanish, Russian, Korean.
- **Dev Mode Visibility**: Landing page indicates when Redis fallback (in-memory quota) is active.
- **Preflight Status**: `/api/status` endpoint powers instant gating (can_search, turnstile_required).
- **SSR Hydration**: Initial status and tier are pre-rendered on the server.
- **Accessibility**: "How to use" icon with `aria-label`; Message Board uses `role="status"`.
- **PostGIS-backed Search**: Uses Neon PostgreSQL with PostGIS and SQLAlchemy 2 async engine (NullPool + pre_ping).
- **Strict DTOs**: Public responses use meters (`distance_m`) and typed URLs; extra fields are forbidden.

## Architecture
The project follows a Domain-First architecture using FastAPI, Redis (Upstash), and PostGIS (Neon PostgreSQL) for POIs, with server-rendered Jinja2. Database access is standardized via SQLAlchemy 2 async engine with `NullPool` and `pool_pre_ping`, and the engine is disposed in app shutdown.

## Developer Guide
For a detailed introduction to the codebase, modules, and architecture, please read the **[Developer Introduction & Architecture Guide](DEVELOPER_GUIDE.md)**.

## Getting Started

1.  **Clone the repo**.
2.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
    On Windows, building `asyncpg` may require Microsoft C++ Build Tools. Alternatively, use WSL/Linux or Python 3.11/3.12 environments where prebuilt wheels are available.
3.  **Configure Environment**:
    Create a `.env` file (Redis/Turnstile optional; DB recommended):
    ```env
    DATABASE_URL="postgresql://user:pass@host/db?sslmode=require"
    ENABLE_REDIS="false"
    UPSTASH_REDIS_REST_URL=""
    UPSTASH_REDIS_REST_TOKEN=""
    CLOUDFLARE_TURNSTILE_SECRET=""
    CLOUDFLARE_TURNSTILE_SITE_KEY=""
    ENV="development" # or "production"
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
