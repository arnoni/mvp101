````md
# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

---

# Project Summary

DillDrill is a privacy-focused, URL-first radar for detecting construction noise and real-estate POIs in Da Nang, Vietnam. Built on FastAPI with PostGIS (Neon PostgreSQL) for spatial search and Redis (Upstash) for quota enforcement. Anonymous users get tiered access (Free: 2 searches/day, 1 result; Paid: 50/day, 5 results).

---

# Core Architectural Invariants (Must Never Change Without Explicit Approval)

1. Quota enforcement is centralized in `PolicyEngine`.
2. EntitlementService never trusts session tier.
3. Redis is authoritative for quota tracking.
4. PostGIS performs all spatial filtering.
5. Middleware execution order must remain as defined.
6. Fail-closed behavior in production must be preserved.
7. All request I/O is async.
8. No precise coordinates logged with identity.

If any change threatens these invariants, stop and request approval.

---

# Common Commands (UV Only)

This repo uses **uv** for dependency management and running commands.

```bash
# Install dependencies (preferred, lockfile-based)
uv sync

# Fallback only when explicitly needed (requirements.txt based)
uv pip install -r requirements.txt

# Run dev server
uv run uvicorn app.main:app --reload

# Lint (ruff: E, F, I, UP, B rules; line-length=100; ignores E501)
uv run ruff .

# Import guard — fails if sqlalchemy.orm.Session imported outside migrations
uv run python check_no_session_import.py

# Run tests
uv run pytest tests/
uv run pytest tests/test_quota_logic.py       # quota key scoping
uv run pytest tests/test_middleware_order.py  # middleware execution order
uv run pytest tests/test_ugc_plan_v2.py       # UGC dedup and validation

# Health checks
curl http://localhost:8000/health
curl http://localhost:8000/health/db
````

---

# UV Rules (Non-Negotiable)

* Use **uv** for all dependency operations and command execution.
* Do not use `pip install` directly.
* Prefer `uv sync` to reproduce environments from `uv.lock`.
* If dependencies change, update `uv.lock` and keep the diff minimal.
* `requirements.txt` exists for compatibility, but `uv.lock` is the source of truth.

---

# Architecture

## Service-Layer Pattern

Routes are thin HTTP handlers. Business logic lives in `app/services/`.

```
Client → Middleware Stack → API Routes → Services → PostGIS / Redis
```

---

## Middleware execution order (last added runs first)

1. **SessionMiddleware** — hydrates `user_id`, `csrf` from Redis session
2. **EntitlementMiddleware** — computes `tier` via EntitlementService (never trusts session payload)
3. **AnonIdMiddleware** — ensures HMAC-signed `dd_anon_id` cookie exists
4. **LoggingMiddleware** — structlog wrapper

Middleware order changes require test updates and explicit approval.

---

# Key Services (`app/services/`)

* **PolicyEngine** (`policy_engine.py`) — gate logic: returns ALLOW/BLOCK/CHALLENGE_REQUIRED. Checks tier + quota + Turnstile requirement. Does not execute searches.
* **BucketEngine** (`bucket_engine.py`) — Aggregates user location to 500m grid cells.
* **PrecomputeRepository** (`precompute_repo.py`) — Fetches opaque, pre-calculated candidates from Postgres JSONB. Replaces live PostGIS search.
* **POIService** (`poi_service.py`) — **DEPRECATED**. Legacy PostGIS search; retained for admin tooling only.
* **QuotaRepository** (`quota_repository.py`) — Redis quota tracking with atomic Lua script. Fail-closed in production.
* **EntitlementService** (`entitlement_service.py`) — tier resolution from Redis cache with monotonic staleness checks. Self-heals corrupt JSON. Falls back to FREE on miss.
* **i18n** (`i18n.py`) — EN, ES, RU, KO translations with LRU process cache + optional warmup.

---

# Dependency Injection

Services are wired via FastAPI `Depends()` in `app/api/dependencies.py`. Routes receive fully constructed services.

No direct service instantiation inside route handlers.

---

# API Endpoints (`app/api/routes.py`)

* `GET /api/status` — preflight gating without consuming quota
* `POST /api/find-nearest` — main search (lat, lon, optional turnstile_token) -> Returns OPAQUE report lines.
* `GET /api/download-kmz` — **REMOVED** (Legacy)
* `POST /api/ugc/report-submit` — UGC noise reports (Turnstile mandatory, no admin bypass)

---

# Quota Identity Rule

If `tier == PAID` and entitlement is fresh and `user_id` exists → user-scoped quota key. Otherwise → anon-scoped key. This prevents stale paid sessions from consuming paid quota.

---

# Critical Design Rules

* **Input**: Accept `lat`/`lon` floats only. No address geocoding.
* **Privacy**: Never log precise coordinates with user identity. Use `AreaBucketer` for spatial aggregation.
* **DTOs**: Public responses use `extra="forbid"`. Distances in meters (`distance_m`). Links as `HttpUrl`.
* **Logging**: Use `structlog` only, not standard `logging`.
* **Fail-closed quota**: Redis down in production → requests blocked (no in-memory fallback).
* **Bounding box**: All coordinates validated against Da Nang bbox `[108.10, 16.00, 108.30, 16.12]`.
* **All I/O is async**: SQLAlchemy async engine, redis.asyncio, httpx.AsyncClient.
* **Cookie auth**: `dd_anon_id` (HMAC-signed UUID, 730d), `dd_session` (Redis-backed), `dd_lang` (language preference).

---

# Database

Neon PostgreSQL with PostGIS. SQLAlchemy 2 async engine with NullPool + pool_pre_ping. Tables: `pois` (spatial), `users`, `subscriptions`, `webhook_events`, `ugc_reports`, `ugc_report_evidence`. Use `text()` with bound parameters for SQL — never concatenate user input.

---

# Security Boundaries

* All user input must be validated via Pydantic models.
* No string concatenation for SQL queries.
* HMAC comparisons must remain constant-time.
* Cookies must preserve current expiry durations.
* CSRF validation skipped by design (rely on Turnstile + Origin check).
* Never bypass Turnstile.
* Never remove atomic Redis Lua script usage.
* Never trust client-provided tier.
* Never expose internal Redis keys in responses.

---

# Performance Constraints

* Spatial queries must use PostGIS indexes.
* Avoid full table scans on `pois`.
* Redis operations must remain O(1) per request.
* Avoid N+1 DB queries.
* No blocking network calls in middleware.
* Avoid large global initializations (cold start sensitive).

---

# Testing Philosophy

* Every change touching quota, middleware, or entitlement must include a test.
* Middleware order changes require explicit test updates.
* Never remove tests to silence failures.
* UGC validation logic must remain deterministic.
* Run tests before declaring completion.

---

# Change Discipline

Before making non-trivial changes:

1. Summarize the goal in 3 bullet points.
2. Identify impacted services or middleware.
3. Evaluate privacy implications.
4. Evaluate quota impact.
5. Keep diffs minimal and localized.
6. Run tests.
7. Summarize changes clearly.

Large refactors require explicit approval.

---

# Environment Rules

Production:

* Must fail-closed.
* No debug logging.
* No relaxed quota behavior.

Development:

* Verbose logging allowed.
* Feature flags acceptable.
* Production Redis key structure must not change without migration.

---

# Cost Constraints

* Avoid adding per-request external API calls.
* Avoid high-frequency Redis writes.
* Prefer caching when safe.
* Avoid expensive spatial queries without bounding.

---

# Definition of Done

A change is complete only if:

* All tests pass.
* Lint passes.
* No privacy regression.
* No quota regression.
* Middleware order preserved.
* Async integrity preserved.
* Diff minimal and documented.
* Dependency changes update `uv.lock` and remain minimal.

---

# Agent Guardrails (Non-Negotiable)

Claude must never:

* Weaken quota enforcement.
* Relax privacy protections.
* Remove fail-closed behavior.
* Replace async with sync.
* Introduce SQL injection risk.
* Modify production deployment config without instruction.
* Bypass Turnstile.
* Modify Redis Lua atomicity logic.
* Log precise coordinates with identity.
* Use pip directly instead of uv.

If uncertain about architectural impact, propose a plan first.

---

# Deployment

Deployed to **Vercel** (`vercel.json`), Python 3.12 runtime. Environment configured via `.env` locally or Vercel dashboard in production.

No secrets committed to repository.
