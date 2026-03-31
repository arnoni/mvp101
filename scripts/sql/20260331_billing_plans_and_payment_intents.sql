CREATE TABLE IF NOT EXISTS billing_plans (
    code TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    display_price TEXT NOT NULL,
    amount_usd_cents INTEGER NOT NULL CHECK (amount_usd_cents > 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    dodo_product_id TEXT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO billing_plans (code, display_name, display_price, amount_usd_cents, currency, is_active)
VALUES
    ('1_day', '1 Day Pass', '$4.99', 499, 'USD', TRUE),
    ('3_day', '3 Day Pass', '$9.99', 999, 'USD', TRUE)
ON CONFLICT (code) DO UPDATE
SET
    display_name = EXCLUDED.display_name,
    display_price = EXCLUDED.display_price,
    amount_usd_cents = EXCLUDED.amount_usd_cents,
    currency = EXCLUDED.currency,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

CREATE TABLE IF NOT EXISTS payment_intents (
    id UUID PRIMARY KEY,
    email TEXT NOT NULL,
    plan_code TEXT NOT NULL REFERENCES billing_plans(code),
    amount_usd_cents INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL,
    provider_event_id TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payment_intents_email_created_at
ON payment_intents (email, created_at DESC);
