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
