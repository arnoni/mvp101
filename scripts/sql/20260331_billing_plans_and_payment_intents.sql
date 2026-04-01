ALTER TABLE users ADD COLUMN IF NOT EXISTS provider_customer_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_provider_customer_id_unique ON users(provider_customer_id) WHERE provider_customer_id IS NOT NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS ab_cohort TEXT NOT NULL DEFAULT 'A';
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ NULL;

DROP TABLE IF EXISTS subscriptions;

CREATE TABLE IF NOT EXISTS free_quotas (
    cohort TEXT PRIMARY KEY,
    daily_limit INTEGER NOT NULL CHECK (daily_limit > 0)
);

INSERT INTO free_quotas (cohort, daily_limit)
VALUES ('A', 3), ('B', 4)
ON CONFLICT (cohort) DO UPDATE
SET daily_limit = EXCLUDED.daily_limit;

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

CREATE INDEX IF NOT EXISTS idx_payment_intents_user_created_at
ON payment_intents (user_id, created_at DESC);

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

CREATE INDEX IF NOT EXISTS idx_user_passes_user_id
ON user_passes USING BTREE (user_id);
