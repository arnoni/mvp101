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

CREATE INDEX IF NOT EXISTS idx_user_location_queries_created_at
    ON user_location_queries (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_location_queries_anon_created
    ON user_location_queries (anon_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_location_queries_session_created
    ON user_location_queries (session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_location_queries_location
    ON user_location_queries
    USING GIST (location);

CREATE INDEX IF NOT EXISTS idx_user_location_queries_demand_cell_created
    ON user_location_queries (demand_cell_id, created_at DESC);
