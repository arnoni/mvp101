# DillDrill Refactor Gap Report (Spec vs Current Codebase)

Date: 2026-03-29
Scope scanned: FastAPI routes, DTOs/schemas, service boundaries, frontend flow, and Redis usage.

## Executive Summary

The current codebase is **partially aligned** with the target architecture but still reflects a pre-refactor model:

- Search is still centered on legacy/per-gauge routes (`/api/find-nearest` in backend; `/api/construction` and `/api/demand` in frontend).
- DTOs are still `FindNearest*` + legacy status/report formats, not the new `Search*`/`GaugeResult`/`UserReport*` contract.
- User report paths and payloads are inconsistent (`/api/ugc/report-submit` backend vs `/api/reports` frontend).
- Billing routes in the spec are not present; frontend calls non-canonical `/billing/*` endpoints.
- Webhook routes exist and are already separate from auth routes (good), but webhook logic still directly sends magic links.
- Entitlement logic exists but is still helper-style (tier mapping/cache), not yet a full service boundary matching the target method list.
- Redis is used for quota/entitlements and webhook lock/idempotency patterns, but not yet for canonical `search` result caching + in-flight dedup lock semantics.

---

## Gap Matrix (12 Required Areas)

### 1) One public search endpoint (`POST /api/search`)

**Current state**
- Backend public search route is `POST /api/find-nearest`. No `POST /api/search` route exists.
- Frontend uses `POST /api/construction` and `POST /api/demand`, which are not defined in current FastAPI router table in `app/api/routes.py`.

**Gap**
- Canonical search endpoint is missing.
- Frontend and backend route contracts are mismatched.

**Refactor requirement**
- Add `POST /api/search` with `target` support (`construction|demand|both`) and migrate frontend calls.
- Keep legacy routes only as temporary wrappers (if needed) with deprecation logs.

### 2) Independent frontend gauge animations

**Current state**
- Frontend already maintains independent `construction` and `demand` state slices and independent async calls/animations.
- Demand currently has a hard dependency on construction success for same coord key (`Run construction first`).

**Gap**
- Independence is partially implemented in UI state/animation, but request orchestration is tied to separate endpoints and a construction-first gate in the client.

**Refactor requirement**
- Keep animation independence but call a single `/api/search` endpoint with target-specific requests.
- Remove client assumption that demand must always follow construction if product no longer mandates that sequence.

### 3) Auth and billing fully separated

**Current state**
- Auth routes exist at `/api/auth/login`, `/api/auth/magic`, `/api/auth/logout`.
- Frontend checkout flow calls `/billing/unlock_intent` and `/billing/resend_magic_link` (non-API-prefixed and currently not found in FastAPI routes scanned).
- Auth login schema includes optional purchase/provider linkage.

**Gap**
- Canonical billing route (`/api/billing/unlock-intent`) is absent.
- Naming and route prefixes are inconsistent with spec.
- Some purchase coupling appears in auth request model/service usage.

**Refactor requirement**
- Introduce dedicated billing router/service (`/api/billing/unlock-intent`) and move all billing-only concerns there.
- Shift magic-link resend into `/api/auth/magic-link` and keep billing endpoint free of auth behavior.

### 4) Webhook kept as provider integration

**Current state**
- Webhook router exists at `/api/webhooks` with provider-specific endpoints (`/paddle`, `/dodo`).
- This is already structurally separate from auth routes.

**Gap**
- Route naming in spec expects canonical `/api/webhooks/dodo` as primary; current code has both providers active.
- Signature verification methods are placeholder/no-op in production branches (return false when not development), which may block real events unless replaced.

**Refactor requirement**
- Keep webhook as provider integration boundary.
- Implement production-grade signature verification and idempotency abstraction under `WebhookService`.

### 5) Entitlements as their own service concern

**Current state**
- `EntitlementService` exists with tier mapping and Redis cache helpers.
- Request pipeline uses middleware/policy checks, but entitlement boundary is still narrow and not represented with the target method contract/state object.

**Gap**
- Missing dedicated entitlement domain API (`get_current_entitlements`, per-target allowed checks, activation from purchase).
- Entitlement behavior is spread across middleware/policy/auth/webhook flows.

**Refactor requirement**
- Expand entitlement service into a first-class domain service with clear DTO/state and explicit methods consumed by search/status/webhook flows.

### 6) `/api/user-reports` instead of `/api/reports`

**Current state**
- Backend route present: `POST /api/ugc/report-submit`.
- Frontend route called: `POST /api/reports`.
- No `/api/user-reports` route found.

**Gap**
- Both backend and frontend are off-spec and inconsistent with each other.

**Refactor requirement**
- Add `POST /api/user-reports` with new DTO.
- Provide temporary adapters from legacy paths during migration; then remove.

### 6.1) API Versioning vs in-place replacement (cross-cutting for routes/DTOs)

**Current state**
- Current API shape is already consumed by existing frontend code with legacy route and payload assumptions.
- Proposed refactor changes both endpoint paths and response DTO structures.

**Gap**
- Wrapper-only migration can still create accidental contract bleed if old and new DTO handling coexist under the same base path.

**Refactor requirement**
- Evaluate introducing a versioned cut (e.g., `/api/v2/search`, `/api/v2/user-reports`, `/api/v2/billing/unlock-intent`, `/api/v2/auth/magic-link`) while preserving `/api/*` current behavior during migration.
- If versioning is not chosen, require explicit compatibility tests and telemetry guards for every legacy wrapper before removal.

### 7) Revised DTOs

**Current state**
- DTO module contains `FindNearestRequest`, `FindNearestResponse`, `StatusResponse`, `ErrorResponse`.
- DTOs do not include `SearchTarget`, `GaugeResult`, `SearchResponse`, `UnlockIntent*`, `MagicLinkRequest`, `UserReportRequest/Response` as specified.

**Gap**
- Contract is still legacy and not compatible with spec response shape/message placement.

**Refactor requirement**
- Introduce new schema modules (`schemas/search.py`, `schemas/auth.py`, `schemas/billing.py`, etc.) and transition route response models.

### 8) Revised report options (3 enums only)

**Current state**
- UI report choices currently include: `active_construction`, `noise_heard`, `new_site_spotted`, `unsure_but_suspicious`.
- Backend UGC request model does not use `report_kind`; it accepts title/description/category/severity/evidence URLs.

**Gap**
- Report taxonomy is not aligned (extra options, wrong field model).

**Refactor requirement**
- Restrict to `active_construction | maybe_construction | construction_ended`.
- Map these cleanly to UI labels and persist enum values only.

### 9) Added `is_nearby_now` checkbox support

**Current state**
- Report modal has no nearby checkbox field.
- Frontend submission payload does not include `is_nearby_now`.
- Backend UGC request model has no `is_nearby_now` field.

**Gap**
- Feature is absent in both UI and API layers.

**Refactor requirement**
- Add checkbox UI and pass `is_nearby_now` in `UserReportRequest`.

### 9.1) Database migration requirements for report taxonomy + nearby flag

**Current state**
- Existing UGC persistence expects free-form title/description/category/severity/evidence fields and does not represent the strict new `report_kind` + `is_nearby_now` schema.

**Gap**
- The spec-level DTO change requires data-model and storage updates that are not yet planned in this report.
- Legacy report rows using old categories (e.g., `noise_heard`, `new_site_spotted`, `unsure_but_suspicious`) need an explicit mapping/backfill strategy.

**Refactor requirement**
- Add a DB migration step (Alembic or SQL migration script) to:
  1. add `report_kind` constrained enum (`active_construction|maybe_construction|construction_ended`)
  2. add `is_nearby_now` boolean with default `false`
  3. backfill legacy records using a documented mapping policy
  4. preserve auditability of original values (raw legacy field retained or copied to an archival column)
- Require a rollback plan for migration safety in production.

### 10) Gauge-specific messages inside `construction` and `demand`

**Current state**
- Current frontend expects top-level `message` from dedicated per-gauge endpoints.
- Legacy search DTO (`FindNearestResponse`) has no nested gauge objects.

**Gap**
- Message placement is incompatible with target contract.

**Refactor requirement**
- Move to nested `construction.message(_code)` and `demand.message(_code)` in `SearchResponse`.

### 11) Optional top-level message for whole-request state

**Current state**
- No canonical `SearchResponse` top-level `message_code` + `message` envelope.
- Existing status/report DTO patterns use different field semantics.

**Gap**
- Request-wide message contract missing.

**Refactor requirement**
- Add optional top-level message fields to `SearchResponse` and enforce precedence in frontend rendering.

### 12) Redis result cache and in-flight dedup guidance

**Current state**
- Redis is used for quota repository and entitlement caching.
- Webhooks implement transient Redis lock for event processing.
- Search flow (`/find-nearest`) does not implement Redis response cache or search in-flight dedup lock keyed by target/tier/location.

**Gap**
- Core search cache/dedup strategy from spec is not implemented.

**Refactor requirement**
- Add search cache key and lock key strategy in `SearchService` with TTLs (~120s cache, ~10-20s lock).

### 12.1) Quota/rate-limit consumption semantics in unified search flow

**Current state**
- Quota policy exists in current stack, but unified-search billing/quota semantics are not yet defined in this report.

**Gap**
- Missing deterministic rules for when credits are consumed across:
  - `target=construction|demand|both`
  - cache hit vs cache miss
  - lock contention/retry responses
  - blocked/denied requests

**Refactor requirement**
- Define and implement explicit quota rules in `SearchService`/`QuotaService`, for example:
  1. deny path: consume 0 credits when request is blocked before compute
  2. cache-hit path: consume 0 or discounted credits (policy decision must be explicit)
  3. cache-miss compute path: consume according to target (`construction=1`, `demand=1`, `both=2` or bundled rate)
  4. lock-contention path: consume 0 credits for retry/processing responses
- Add observability fields on responses/logs so finance/policy can audit cache-vs-consumption behavior.

---

## Additional Structural Observations

1. **Router structure** does not yet match proposed modular layout (`search.py`, `billing.py`, `user_reports.py`, etc.); much logic is concentrated in `app/api/routes.py`.
2. **Service boundaries** are mixed: route handlers are heavy and include orchestration/database details (especially UGC and find-nearest).
3. **Frontend/backend contract drift** exists in multiple places (`/api/reports`, `/billing/*`, `/api/construction`, `/api/demand`), indicating migration partially started but incomplete.
4. **Deprecation sequencing** from the spec should be explicitly tracked to avoid regressions during route renaming.

---

## Recommended Next Refactoring Slice (Practical Order)

1. **Introduce canonical schemas first** (`SearchRequest/Response`, `GaugeResult`, `UserReport*`, billing/auth DTOs).
2. **Draft and execute DB migrations** (Alembic/SQL) for new `report_kind` enum + `is_nearby_now` boolean, including legacy backfill mappings and rollback notes.
3. **Decide migration boundary strategy**: `/api/v2/*` versioned rollout vs in-place replacement with strict compatibility wrappers.
4. **Ship `POST /api/search` route + `SearchService` skeleton** with target branching (construction/demand/both), stable response shape, and explicit quota-consumption semantics.
5. **Implement Redis cache + in-flight lock for search** behind feature flag, including lock-timeout/failure behavior tests.
6. **Add `/api/user-reports` + slim `UserReportService`** using three report kinds + `is_nearby_now`.
7. **Create `/api/billing/unlock-intent` and `/api/auth/magic-link`**; move old flows behind temporary compatibility wrappers (or `/api/v2/*` cutover).
8. **Migrate frontend calls** to canonical new routes and DTOs.
9. **Apply deprecation policy**: log old endpoints, monitor usage for 14 days, then remove once traffic reaches zero.

---

## Suggested Acceptance Checklist for the Refactor PR

- [ ] `POST /api/search` is the only public search route used by frontend.
- [ ] Search request supports `target=construction|demand|both`.
- [ ] Frontend construction and demand animations remain independently controlled.
- [ ] `POST /api/user-reports` implemented with 3 enum values + `is_nearby_now`.
- [ ] Gauge messages are nested per gauge; top-level message optional and request-wide only.
- [ ] `/api/billing/unlock-intent` and `/api/auth/magic-link` are cleanly separated.
- [ ] Webhook route remains provider integration and not merged into auth.
- [ ] Redis search result caching + in-flight dedup are implemented and tested.
- [ ] Quota consumption behavior is explicitly defined and tested for cache-hit/miss, `target=both`, deny, and lock-contention paths.
- [ ] DB migrations are applied and validated for report taxonomy + `is_nearby_now` (including legacy mapping/backfill verification).
- [ ] Automated tests are added/updated:
  - unit tests for `SearchService` orchestration
  - DTO validation tests (request/response models)
  - integration tests for Redis caching/locking behavior
- [ ] Legacy routes have temporary wrappers + deprecation logging.
