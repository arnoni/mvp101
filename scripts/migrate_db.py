import asyncio
import os
import sys

# Ensure project root is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
from app.core.config import settings

async def run_migrations():
    print(f"Connecting to database: {settings.DATABASE_URL}")
    if not settings.DATABASE_URL:
        print("DATABASE_URL not set. Skipping migrations.")
        return

    # Create async engine
    url = settings.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
    if "sslmode=" not in url:
        url += "&sslmode=require" if "?" in url else "?sslmode=require"
    engine = create_async_engine(url, echo=True)

    sql_statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email TEXT UNIQUE NOT NULL,
            provider_customer_id TEXT UNIQUE,
            ab_cohort TEXT NOT NULL DEFAULT 'A',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login TIMESTAMPTZ NULL
        );
        """,
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS provider_customer_id TEXT;",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_provider_customer_id_unique ON users(provider_customer_id) WHERE provider_customer_id IS NOT NULL;",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS ab_cohort TEXT NOT NULL DEFAULT 'A';",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ NULL;",
        "DROP TABLE IF EXISTS subscriptions;",
        """
        CREATE TABLE IF NOT EXISTS free_quotas (
            cohort TEXT PRIMARY KEY,
            daily_limit INTEGER NOT NULL CHECK (daily_limit > 0)
        );
        """,
        """
        INSERT INTO free_quotas (cohort, daily_limit)
        VALUES ('A', 3), ('B', 4)
        ON CONFLICT (cohort) DO UPDATE
        SET daily_limit = EXCLUDED.daily_limit;
        """,
        """
        CREATE TABLE IF NOT EXISTS magic_link_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES users(id),
            email TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, 
            purpose TEXT NOT NULL DEFAULT 'login',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            redeemed_at TIMESTAMPTZ,
            requester_ip_hash TEXT,
            user_agent_hash TEXT
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_magic_tokens_email_created 
        ON magic_link_tokens(email, created_at);
        """,
        """
        CREATE TABLE IF NOT EXISTS cell_poi_precompute (
            cell_id TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,

        """
        CREATE TABLE IF NOT EXISTS webhook_events (
            provider TEXT NOT NULL,
            event_id TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            processed_at TIMESTAMPTZ,
            payload JSONB NOT NULL,
            PRIMARY KEY (provider, event_id)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS user_location_queries (
            id BIGSERIAL PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            anon_id TEXT NULL,
            session_id TEXT NULL,
            user_id BIGINT NULL,
            input_kind TEXT NOT NULL,
            original_input TEXT NOT NULL,
            source_url TEXT NULL,
            normalized_input TEXT NOT NULL,
            latitude DOUBLE PRECISION NOT NULL,
            longitude DOUBLE PRECISION NOT NULL,
            location GEOGRAPHY(POINT, 4326) NOT NULL,
            demand_cell_id TEXT NULL,
            request_country TEXT NULL,
            request_city TEXT NULL,
            user_agent TEXT NULL,
            result_status TEXT NOT NULL,
            result_count INTEGER NULL,
            error_code TEXT NULL,
            response_ms INTEGER NULL,
            is_duplicate_window BOOLEAN NOT NULL DEFAULT FALSE
        );
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_user_location_queries_created_at
        ON user_location_queries (created_at DESC);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_user_location_queries_anon_created
        ON user_location_queries (anon_id, created_at DESC);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_user_location_queries_session_created
        ON user_location_queries (session_id, created_at DESC);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_user_location_queries_location
        ON user_location_queries USING GIST (location);
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_user_location_queries_demand_cell_created
        ON user_location_queries (demand_cell_id, created_at DESC);
        """,
        """
        CREATE TABLE IF NOT EXISTS billing_plans (
            code TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            display_price TEXT NOT NULL,
            amount_usd_cents INTEGER NOT NULL CHECK (amount_usd_cents > 0),
            duration_days INTEGER NOT NULL CHECK (duration_days > 0),
            daily_limit INTEGER NOT NULL CHECK (daily_limit > 0),
            currency TEXT NOT NULL DEFAULT 'USD',
            dodo_product_id TEXT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "ALTER TABLE billing_plans ADD COLUMN IF NOT EXISTS duration_days INTEGER;",
        "ALTER TABLE billing_plans ADD COLUMN IF NOT EXISTS daily_limit INTEGER;",
        "UPDATE billing_plans SET duration_days = 1 WHERE duration_days IS NULL;",
        "UPDATE billing_plans SET daily_limit = 8 WHERE daily_limit IS NULL;",
        "ALTER TABLE billing_plans ALTER COLUMN duration_days SET NOT NULL;",
        "ALTER TABLE billing_plans ALTER COLUMN daily_limit SET NOT NULL;",
        """
        INSERT INTO billing_plans (code, display_name, display_price, amount_usd_cents, duration_days, daily_limit, currency, is_active)
        VALUES
            ('1_day_test_a', '1 Day Pass', '$4.99', 499, 1, 10, 'USD', TRUE),
            ('1_day_test_b', '1 Day Pass', '$3.99', 399, 1, 8, 'USD', TRUE),
            ('3_day_test_a', '3 Day Pass', '$9.99', 999, 3, 15, 'USD', TRUE),
            ('3_day_test_b', '3 Day Pass', '$7.99', 799, 3, 12, 'USD', TRUE)
        ON CONFLICT (code) DO UPDATE
        SET
            display_name = EXCLUDED.display_name,
            display_price = EXCLUDED.display_price,
            amount_usd_cents = EXCLUDED.amount_usd_cents,
            duration_days = EXCLUDED.duration_days,
            daily_limit = EXCLUDED.daily_limit,
            currency = EXCLUDED.currency,
            is_active = EXCLUDED.is_active,
            updated_at = NOW();
        """,
        """
        CREATE TABLE IF NOT EXISTS payment_intents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            plan_code TEXT NOT NULL REFERENCES public.billing_plans(code),
            amount_cents INTEGER NOT NULL,
            provider_intent_id TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            currency TEXT NOT NULL DEFAULT 'USD',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES public.users(id) ON DELETE CASCADE;",
        "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS amount_cents INTEGER;",
        "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS provider_intent_id TEXT;",
        "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'USD';",
        "ALTER TABLE payment_intents ADD COLUMN IF NOT EXISTS plan_code TEXT REFERENCES public.billing_plans(code);",
        
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_intents_provider_intent_id ON payment_intents(provider_intent_id) WHERE provider_intent_id IS NOT NULL;",
        """
        CREATE INDEX IF NOT EXISTS idx_payment_intents_user_created_at
        ON payment_intents (user_id, created_at DESC);
        """,
        """
        CREATE TABLE IF NOT EXISTS user_passes (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
            plan_code TEXT NOT NULL REFERENCES public.billing_plans(code),
            provider_payment_id TEXT UNIQUE NOT NULL,
            amount_paid_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_user_passes_user_id ON user_passes USING BTREE (user_id);",
    ]

    try:
        async with engine.begin() as conn:
            for statement in sql_statements:
                try:
                    print(f"Executing: {statement[:50]}...")
                    await conn.execute(text(statement))
                except Exception as e:
                    print(f"\nSQL Execution Failed!")
                    print(f"Statement: {statement}")
                    print(f"Error: {e}")
                    await conn.rollback()
                    sys.exit(1)
    except Exception as e:
        print(f"Transaction failed: {e}")
        sys.exit(1)

    print("Migrations complete.")
    await engine.dispose()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_migrations())
