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
