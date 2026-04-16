# SQL Schema Pre-Modification Decisions & Implementation Plan (Pre-Launch Fast Path)

## Goal
Because the product is **pre-launch** and the database contains only disposable test data, this plan intentionally uses a **fast-path reset strategy** (not zero-downtime migration choreography). We optimize for correctness and speed before real users arrive.

## A) Business-logic / product-design decisions to make first

1. **Identity model source of truth**
   - Confirm `users.id` is UUID everywhere (API, Redis payloads, webhook payload mapping, analytics events).
   - Confirm anonymous-to-auth transition rules for historical records.

2. **Magic-link architecture (Redis-only vs Postgres-backed)**
   - Decide one source of truth for token lifecycle.
   - Define retention/audit expectations (security review + incident forensics).

3. **UGC evidence durability policy**
   - Decide whether evidence is durable in SQL or intentionally ephemeral cache data.
   - Define retention/legal moderation windows.

4. **UGC deduplication contract**
   - Decide strict vs fuzzy dedupe and time-window behavior.
   - Decide whether duplicates are blocked, linked, or both.

5. **Webhook ledger semantics**
   - Choose: idempotency-only ledger vs full lifecycle ledger with retries/errors.
   - If lifecycle, define minimum observability fields.

6. **Status vocabulary governance**
   - Freeze allowed values for status-like columns and decide enum vs `CHECK` constraints.

7. **Pricing/currency model**
   - Decide single-currency vs multi-currency model now, before names fossilize.

8. **Retention & deletion policy**
   - Set retention classes for auth/payment/UGC/query telemetry.
   - Choose hard-delete vs soft-delete by entity.

9. **Authorization boundary: RLS vs app-layer authorization**
   - Explicitly decide whether authorization is enforced primarily via:
     - Postgres Row Level Security (RLS), or
     - Python service-layer checks.
   - Define ownership checks, service-role exceptions, and testing strategy accordingly.

10. **JSONB governance for `cell_poi_precompute.data`**
    - Decide canonical schema contract for the JSONB payload.
    - Define validation strategy (e.g., Pydantic model + versioned schema key in JSON).

11. **Operational consistency policy (DB vs cache ordering)**
    - Decide mandatory ordering for writes (DB commit before cache publish, or outbox/event pattern).

## B) Missing SQL tables worth adding now

1. **`outbox_events` (recommended)**
   - Durable post-commit event queue for cache updates/jobs/webhooks.

2. **`webhook_processing_attempts`**
   - Attempt-level retry/error history for webhook operations.

3. **`schema_migration_audit`** (if migration framework does not already provide equivalent metadata)
   - Immutable record of applied migration id/checksum/operator/time.

4. **`report_dedupe_keys`** (optional)
   - Durable dedupe claims when Redis-only dedupe is insufficient.

5. **`user_entitlement_events`**
   - Append-only entitlement history for debugging and reconciliation.

6. **`auth_token_events`** (if using SQL-backed magic links)
   - Creation/redemption/invalidation trail.

## C) Implementation plan (pre-launch fast path; do not execute yet)

### Phase 0 — Finalize decisions (short, explicit)
- Resolve section A decisions in one architecture review.
- Produce a one-page “Data Contract v1” covering identity type, status values, auth boundary (RLS/app), JSONB schema policy, and retention.

### Phase 1 — Lock the corrected target schema (V1)
- Apply immediate schema corrections directly (no ZDM overhead):
  - Fix `ugc_reports.reporter_user_id` to UUID + FK to `users(id)`.
  - Fix `user_location_queries.user_id` to UUID + FK to `users(id)`.
- Because data is disposable, prefer **drop/recreate** for affected tables if fastest.
- Alternative brute-force path: direct `ALTER TABLE ... TYPE uuid USING ...` and constraint redefinition.

### Phase 2 — Remove pre-launch drift and dead structures
- Decide and enforce whether `magic_link_tokens` is active or removed.
- Decide and enforce whether `ugc_report_evidence` is actively written or removed.
- Make `webhook_events` semantics explicit (idempotency-only vs lifecycle table).

### Phase 3 — Add baseline constraints/indexes immediately
- Add missing FK-support indexes and hot-path indexes.
- Add enum/check constraints for closed status vocabularies.
- Align DB constraints with app validators (e.g., URL length, confidence bounds).

### Phase 4 — Establish migration tooling baseline now (critical)
- Install/standardize Alembic (or chosen migration tool) immediately.
- Generate a clean **V1 initial migration** from the finalized corrected schema.
- Reset dev/test databases from V1 to verify reproducibility.
- Document migration workflow (`upgrade`, `downgrade`, branch strategy, code review checklist).

### Phase 5 — Consistency hardening before launch
- Fix write ordering to prevent cache/DB divergence.
- If needed, introduce `outbox_events` for post-commit side effects.
- Add reconciliation scripts for entitlement/payment mismatches.

### Phase 6 — Readiness gates before inviting real users
- Run integration tests against a freshly migrated database from empty state.
- Verify auth/ownership paths under the chosen RLS/app-authorization model.
- Validate `cell_poi_precompute.data` against the chosen schema validator.
- Freeze schema as “Launch Candidate DB v1”.

## D) Fast-path execution order (recommended)
1. Finalize decisions (including RLS and JSONB governance).
2. Fix UUID mismatches immediately via drop/recreate or direct ALTER.
3. Remove/activate drifted tables (`magic_link_tokens`, `ugc_report_evidence`).
4. Add constraints + indexes.
5. Bootstrap Alembic and create V1 initial migration.
6. Run full reset + migrate + integration test cycle.

## E) Definition of done
- No UUID/BIGINT identity mismatches remain.
- Schema is internally consistent with actual runtime architecture.
- Authorization model (RLS vs app-layer) is explicit and tested.
- JSONB payload contract is versioned and validated.
- Alembic (or chosen tool) is in place with a reproducible V1 baseline migration.
- Fresh-database bootstrap succeeds end-to-end before launch.
