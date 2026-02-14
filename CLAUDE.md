# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Summary

DillDrill is a privacy-focused, URL-first radar for detecting construction noise and real-estate POIs in Da Nang, Vietnam. Built on FastAPI with PostGIS (Neon PostgreSQL) for spatial search and Redis (Upstash) for quota enforcement. Anonymous users get tiered access (Free: 2 searches/day, 1 result; Paid: 50/day, 5 results).

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run dev server
uvicorn app.main:app --reload

# Lint (ruff: E, F, I, UP, B rules; line-length=100; ignores E501)
ruff .

# Import guard — fails if sqlalchemy.orm.Session imported outside migrations
python check_no_session_import.py

# Run tests
pytest tests/
python tests/test_quota_logic.py      # quota key scoping
python tests/test_middleware_order.py  # middleware execution order
python tests/test_ugc_plan_v2.py      # UGC dedup and validation

# Health checks
curl http://localhost:8000/health
curl http://localhost:8000/health/db
```

## Architecture

**Service-Layer Pattern**: Routes are thin HTTP handlers. Business logic lives in `app/services/`.

```
Client → Middleware Stack → API Routes → Services → PostGIS / Redis
```

### Middleware execution order (last added runs first)
1. **SessionMiddleware** — hydrates `user_id`, `csrf` from Redis session
2. **EntitlementMiddleware** — computes `tier` via EntitlementService (never trusts session payload)
3. **AnonIdMiddleware** — ensures HMAC-signed `dd_anon_id` cookie exists
4. **LoggingMiddleware** — structlog wrapper

### Key services (`app/services/`)
- **PolicyEngine** (`policy_engine.py`) — gate logic: returns ALLOW/BLOCK/CHALLENGE_REQUIRED. Checks tier + quota + Turnstile requirement. Does not execute searches.
- **POIService** (`poi_service.py`) — PostGIS spatial search with "Greedy 30m spacing" algorithm (ST_DWithin → order by distance → skip candidates < 30m from selected POIs).
- **QuotaRepository** (`quota_repository.py`) — Redis quota tracking with atomic Lua script. Fail-closed in production.
- **EntitlementService** (`entitlement_service.py`) — tier resolution from Redis cache with monotonic staleness checks. Self-heals corrupt JSON. Falls back to FREE on miss.
- **i18n** (`i18n.py`) — EN, ES, RU, KO translations with LRU process cache + optional warmup.

### Dependency injection
Services are wired via FastAPI `Depends()` in `app/api/dependencies.py`. Routes receive fully constructed services.

### API endpoints (`app/api/routes.py`)
- `GET /api/status` — preflight gating without consuming quota
- `POST /api/find-nearest` — main search (lat, lon, optional turnstile_token)
- `GET /api/download-kmz` — KMZ export (consumes 1 quota unit)
- `POST /api/ugc/report-submit` — UGC noise reports (Turnstile mandatory, no admin bypass)

### Quota identity rule
If `tier == PAID` and entitlement is fresh and `user_id` exists → user-scoped quota key. Otherwise → anon-scoped key. This prevents stale paid sessions from consuming paid quota.

## Critical Design Rules

- **Input**: Accept `lat`/`lon` floats only. No address geocoding.
- **Privacy**: Never log precise coordinates with user identity. Use `AreaBucketer` for spatial aggregation.
- **DTOs**: Public responses use `extra="forbid"`. Distances in meters (`distance_m`). Links as `HttpUrl`.
- **Logging**: Use `structlog` only, not standard `logging`.
- **Fail-closed quota**: Redis down in production → requests blocked (no in-memory fallback).
- **Bounding box**: All coordinates validated against Da Nang bbox `[108.10, 16.00, 108.30, 16.12]`.
- **All I/O is async**: SQLAlchemy async engine, redis.asyncio, httpx.AsyncClient.
- **Cookie auth**: `dd_anon_id` (HMAC-signed UUID, 730d), `dd_session` (Redis-backed), `dd_lang` (language preference).

## Database

Neon PostgreSQL with PostGIS. SQLAlchemy 2 async engine with NullPool + pool_pre_ping. Tables: `pois` (spatial), `users`, `subscriptions`, `webhook_events`, `ugc_reports`, `ugc_report_evidence`. Use `text()` with bound parameters for SQL — never concatenate user input.

## Deployment

Deployed to **Vercel** (`vercel.json`), Python 3.11 runtime. Environment configured via `.env` locally or Vercel dashboard in production.
