# Project TODO

- [TBD] Upgrade admin bypass header for QA/dev superusers
  - Header: X-Admin-Auth
  - Current: Static token comparison via `ADMIN_BYPASS_TOKEN` (bypasses quotas and Turnstile; does not overwrite Redis)
  - Next: Use HMAC-signed token with expiry (e.g., 10–30 mins)
  - Token rotation policy and storage in Vercel env
  - Restrict to specific endpoints and optional IP allowlist
  - Emit audit log event with anon_id and request_id
  - Disabled by default on production unless explicitly enabled

## Implementation Plan (TBD)
- Define token format and signing secret
- Middleware or dependency to validate header
- Add tests and observability (structured logs)
