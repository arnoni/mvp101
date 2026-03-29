BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_report_kind') THEN
        CREATE TYPE user_report_kind AS ENUM (
            'active_construction',
            'maybe_construction',
            'construction_ended'
        );
    END IF;
END $$;

ALTER TABLE ugc_reports
    ADD COLUMN IF NOT EXISTS legacy_category_raw text,
    ADD COLUMN IF NOT EXISTS report_kind user_report_kind,
    ADD COLUMN IF NOT EXISTS is_nearby_now boolean NOT NULL DEFAULT false;

UPDATE ugc_reports
SET legacy_category_raw = COALESCE(legacy_category_raw, category)
WHERE legacy_category_raw IS NULL;

UPDATE ugc_reports
SET report_kind = CASE
    WHEN category = 'active_construction' THEN 'active_construction'::user_report_kind
    WHEN category IN ('noise_heard', 'unsure_but_suspicious') THEN 'maybe_construction'::user_report_kind
    WHEN category = 'new_site_spotted' THEN 'construction_ended'::user_report_kind
    ELSE 'maybe_construction'::user_report_kind
END
WHERE report_kind IS NULL;

ALTER TABLE ugc_reports
    ALTER COLUMN report_kind SET NOT NULL;

COMMIT;
