-- Idempotent: safe to run multiple times
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'ugc_report_status') THEN
    CREATE TYPE ugc_report_status AS ENUM (
      'pending',
      'approved',
      'rejected',
      'flagged'
    );
  END IF;
END
$$;
