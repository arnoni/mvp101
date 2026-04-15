# Service/Route Refactor Checklist for Finalized MVP Decisions

This checklist implements behavioral decisions 9–16.

## 1) Anonymous-to-auth transition (Decision 9)
- `app/services/query_history_repository.py`
  - Keep historical anonymous rows anonymous.
  - Do **not** backfill legacy `anon_id` rows with `user_id` after login.
- `app/api/auth.py`
  - On successful login, only set `user_id` for **new** events/queries.

## 2) Simulated issuance moment (Decision 10)
- `app/services/magic_auth_service.py`
  - Create `simulated_payment_intents` at request/initiation time with status `initiated`/`magic_sent`.
  - Create `simulated_user_passes` **only** after successful token redemption.
- `app/api/auth.py`
  - Token redemption endpoint must be the sole place that transitions simulated intent to `activated` and creates simulated pass.

## 3) Simulated quota resolution (Decision 11)
- `app/services/entitlement_service.py`
  - Add explicit simulated tier resolution path using `simulated_user_passes` + `simulated_billing_plans.daily_limit`.
  - Keep this independent from free-tier quota joins and real paid pass checks.

## 4) Eligibility gating via feature flag (Decision 12)
- `app/services/plan_catalog_service.py`
  - Read `feature_flags.key='simulated_paid_users_allowed_flag'` and expose to callers.
- `app/api/billing.py` and/or unlock endpoints in `app/api/auth.py`
  - Hard-block simulated unlock flow when flag is disabled.

## 5) Webhook isolation (Decision 13)
- `app/api/webhooks.py`
  - Keep webhook handling restricted to real billing events only.
- `app/api/billing.py` / `app/services/magic_auth_service.py`
  - Ensure simulated flows never insert into `webhook_events`.

## 6) App-layer authorization boundary (Decision 14)
- `app/api/dependencies.py`
  - Ensure ownership/identity checks happen in Python dependency layer.
- `app/api/routes.py`, `app/api/auth.py`, `app/api/billing.py`
  - Explicitly enforce user ownership in service/query predicates.
  - Do not assume database RLS policies exist.

## 7) Operational consistency ordering (Decision 15)
- `app/api/webhooks.py`
  - Commit DB state first, then publish/invalidate Redis cache.
- `app/services/entitlement_service.py`
  - Avoid cache updates before transaction commit confirmation.

## 8) Retention strategy (Decision 16)
- `app/services/magic_auth_service.py`
  - Add cleanup job for expired/redeemed token rows.
- `app/services/*` (new scheduled job module)
  - Add retention jobs for stale `simulated_payment_intents` and expired/revoked `simulated_user_passes`.
  - Prefer status transitions/soft retention windows over immediate hard deletes.

## 9) Funnel event emission alignment
- `app/api/routes.py`, `app/api/auth.py`, `app/api/billing.py`
  - Emit `funnel_events` with valid `effective_tier`/`target_tier`/`transition_name` vocabulary.
  - For `event_name='check_completed'`, always supply `related_query_id`.
  - For demand checks, enforce paid/simulated-paid effective tier per DB `CHECK`.
