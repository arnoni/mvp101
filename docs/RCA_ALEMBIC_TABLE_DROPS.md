# Investigation Report: Missing Tables (`cell_poi_precompute`, `webhook_events`, `free_quotas`)

Date: 2026-04-19
Scope: Alembic migrations, ORM model definitions, and Alembic metadata wiring.

## 1) Migration Artifact (Where were these tables dropped?)

### Findings
- Searched Alembic migrations under `alembic/versions/` for:
  - `DROP TABLE`
  - `drop_table(`
  - `cell_poi_precompute`
  - `webhook_events`
  - `free_quotas`
- Result: **No Alembic migration contains a drop for any of these three tables.**

### What Alembic migration files exist
- `22cf0ac436cd_v1_initial_schema.py` (`Revision ID: 22cf0ac436cd`, message: `v1_initial_schema`).
- `bf88275af98e_add_retention_indexes.py` (`Revision ID: bf88275af98e`, message: `add_retention_indexes`).

### What *was* dropped in Alembic
In `22cf0ac436cd_v1_initial_schema.py`, drop operations appear only in the **downgrade** function, and they target:
- `funnel_events`
- `ugc_report_evidence`
- `simulated_user_passes`
- `user_passes`
- `user_location_queries`
- `ugc_reports`
- `simulated_payment_intents`
- `payment_intents`
- `magic_link_tokens`
- `users`
- `simulated_billing_plans`
- `pois`
- `feature_flags`
- `billing_plans`

No entries for `cell_poi_precompute`, `webhook_events`, or `free_quotas` are present in Alembic revisions.

### Non-Alembic drop source discovered
A manual reset document includes explicit drops for all three target tables:
- `docs/V1_BASELINE_DB_BOOTSTRAP.md` contains:
  - `DROP TABLE IF EXISTS webhook_events CASCADE;`
  - `DROP TABLE IF EXISTS free_quotas CASCADE;`
  - `DROP TABLE IF EXISTS cell_poi_precompute CASCADE;`

This indicates a likely operational path where a manual reset script/SQL (not Alembic migration code) removed them.

## 2) ORM Model Existence Check

### Findings
- No SQLAlchemy ORM class in `app/models/` currently declares:
  - `__tablename__ = 'cell_poi_precompute'`
  - `__tablename__ = 'webhook_events'`
  - `__tablename__ = 'free_quotas'`
- The current model registry in `app/models/models.py` includes many entities (`users`, `billing_plans`, `payment_intents`, `user_passes`, etc.) but none of these three tables.

### Additional evidence
- Application code still references these tables using raw SQL text:
  - `cell_poi_precompute` in `app/services/precompute_repo.py`
  - `webhook_events` in `app/api/webhooks.py`
  - `free_quotas` in `app/services/entitlement_service.py`

This means runtime code expects them, but ORM metadata does not define them.

## 3) Alembic Import Wiring / Metadata Aggregator

### Findings
- `alembic/env.py` imports:
  - `from app.models import Base`
  - `import app.models as _models`
  - and sets `target_metadata = Base.metadata`
- `app/models/__init__.py` does import model classes from `app.models.models` into package scope.

### Interpretation
- The Alembic wiring itself is **connected correctly** to `app.models` and `Base.metadata`.
- However, because ORM classes for `cell_poi_precompute`, `webhook_events`, and `free_quotas` do not exist in `app/models/models.py`, they are absent from `Base.metadata`.
- Therefore, from Alembic autogenerate’s perspective, these tables are “not part of the desired schema.”

## Point of Failure (Root Cause)

The failure is not a broken Alembic import path; it is a **schema-definition gap**:

1. The three tables are used by application SQL but are **not represented as ORM models** in Alembic’s metadata source.
2. A documented/manual baseline reset SQL path explicitly drops those tables outside Alembic migration tracking.
3. Once dropped, Alembic migrations in this repo do not recreate them, because they are not in metadata and no migration explicitly creates them.

Net effect: these tables can disappear via manual reset/maintenance workflows, and Alembic won’t restore or protect them because they are outside the modeled schema set.
