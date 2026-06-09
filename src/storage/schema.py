"""PostgreSQL schema for durable scraping state."""

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS schools (
        id BIGSERIAL PRIMARY KEY,
        slug TEXT NOT NULL UNIQUE,
        platform TEXT NOT NULL DEFAULT 'unknown',
        base_url TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scrape_runs (
        id BIGSERIAL PRIMARY KEY,
        school_id BIGINT NOT NULL REFERENCES schools(id),
        mode TEXT NOT NULL DEFAULT 'full',
        status TEXT NOT NULL DEFAULT 'created',
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        target_count INTEGER NOT NULL DEFAULT 0,
        completed_count INTEGER NOT NULL DEFAULT 0,
        failed_count INTEGER NOT NULL DEFAULT 0,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id BIGSERIAL PRIMARY KEY,
        school_id BIGINT NOT NULL REFERENCES schools(id),
        label TEXT NOT NULL,
        secret_ref TEXT,
        status TEXT NOT NULL DEFAULT 'available',
        last_auth_at TIMESTAMPTZ,
        last_success_at TIMESTAMPTZ,
        last_failure_at TIMESTAMPTZ,
        cooldown_until TIMESTAMPTZ,
        max_concurrency INTEGER NOT NULL DEFAULT 1,
        request_rate_per_second NUMERIC(8, 4) NOT NULL DEFAULT 0.2500,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (school_id, label)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
        id BIGSERIAL PRIMARY KEY,
        account_id BIGINT NOT NULL REFERENCES accounts(id),
        status TEXT NOT NULL DEFAULT 'created',
        browser_profile_path TEXT,
        token_expires_at TIMESTAMPTZ,
        last_validated_at TIMESTAMPTZ,
        failure_reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alumni_seed (
        id BIGSERIAL PRIMARY KEY,
        school_id BIGINT NOT NULL REFERENCES schools(id),
        external_user_id TEXT NOT NULL,
        listing_payload JSONB NOT NULL,
        full_name TEXT,
        class_year TEXT,
        source_page INTEGER,
        first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (school_id, external_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_jobs (
        id BIGSERIAL PRIMARY KEY,
        run_id BIGINT NOT NULL REFERENCES scrape_runs(id),
        school_id BIGINT NOT NULL REFERENCES schools(id),
        external_user_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        leased_by TEXT,
        lease_expires_at TIMESTAMPTZ,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TIMESTAMPTZ,
        last_error_code TEXT,
        last_error_message TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (run_id, external_user_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_profile_jobs_claimable
    ON profile_jobs (run_id, status, next_attempt_at, lease_expires_at, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS profile_results (
        id BIGSERIAL PRIMARY KEY,
        run_id BIGINT NOT NULL REFERENCES scrape_runs(id),
        school_id BIGINT NOT NULL REFERENCES schools(id),
        external_user_id TEXT NOT NULL,
        profile_payload_ref TEXT,
        data_payload_ref TEXT,
        normalized_json JSONB NOT NULL,
        field_count INTEGER NOT NULL DEFAULT 0,
        profile_fetched_at TIMESTAMPTZ,
        data_fetched_at TIMESTAMPTZ,
        parser_version TEXT NOT NULL DEFAULT 'v1',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (run_id, external_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_heartbeats (
        worker_id TEXT PRIMARY KEY,
        run_id BIGINT REFERENCES scrape_runs(id),
        account_id BIGINT REFERENCES accounts(id),
        hostname TEXT,
        status TEXT NOT NULL DEFAULT 'starting',
        current_job_id BIGINT,
        completed_count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
]

