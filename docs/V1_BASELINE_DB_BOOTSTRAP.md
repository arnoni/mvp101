# V1 Baseline DB Bootstrap (Pre-Launch Fast Path)

## 0) Assumption
- Pre-launch only.
- Database data is disposable.

## 1) Exact reset SQL (drop all current app tables)

Run this in Neon SQL editor or via `psql`.

```sql
BEGIN;

DROP TABLE IF EXISTS funnel_events CASCADE;
DROP TABLE IF EXISTS simulated_user_passes CASCADE;
DROP TABLE IF EXISTS simulated_payment_intents CASCADE;
DROP TABLE IF EXISTS simulated_billing_plans CASCADE;
DROP TABLE IF EXISTS feature_flags CASCADE;

DROP TABLE IF EXISTS ugc_report_evidence CASCADE;
DROP TABLE IF EXISTS ugc_reports CASCADE;
DROP TABLE IF EXISTS user_location_queries CASCADE;
DROP TABLE IF EXISTS magic_link_tokens CASCADE;
DROP TABLE IF EXISTS payment_intents CASCADE;
DROP TABLE IF EXISTS user_passes CASCADE;
DROP TABLE IF EXISTS webhook_events CASCADE;
DROP TABLE IF EXISTS billing_plans CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS free_quotas CASCADE;
DROP TABLE IF EXISTS cell_poi_precompute CASCADE;
DROP TABLE IF EXISTS pois CASCADE;

COMMIT;
```

Alternative full reset:

```sql
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO postgres;
GRANT ALL ON SCHEMA public TO public;
```

## 2) Exact Alembic commands for V1 baseline

```bash
# 1) install dependency
uv add alembic

# 2) initialize alembic (first time only)
alembic init alembic

# 3) configure alembic:
#    - set sqlalchemy.url in alembic.ini
#    - update alembic/env.py with the exact snippet below

# 4) generate V1 baseline migration
alembic revision --autogenerate -m "v1_initial_schema"

# 5) apply migration
alembic upgrade head
```

## 3) Reproducibility check

```bash
alembic downgrade base
alembic upgrade head
```

If this works repeatedly on an empty DB, your V1 baseline is stable.

## 4) Required `alembic/env.py` registration snippet

Use this so `--autogenerate` sees **all** tables (`feature_flags`, `funnel_events`, simulated tables, etc.).

```python
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

from app.models import Base
import app.models as _models  # noqa: F401  # force model module import side effects

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```
