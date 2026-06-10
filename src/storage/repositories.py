"""Repository methods for durable scraping state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


def _jsonb(value: Any):
    from psycopg.types.json import Jsonb

    return Jsonb(value)


@dataclass
class ScrapeRepository:
    """Small data-access layer around the PostgreSQL schema."""

    conn: Any

    def ensure_school(
        self,
        slug: str,
        base_url: str,
        platform: str = "unknown",
    ) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schools (slug, base_url, platform)
                VALUES (%s, %s, %s)
                ON CONFLICT (slug) DO UPDATE SET
                    base_url = EXCLUDED.base_url,
                    platform = EXCLUDED.platform,
                    enabled = TRUE
                RETURNING *
                """,
                (slug, base_url, platform),
            )
            return cur.fetchone()

    def ensure_account(
        self,
        school_id: int,
        label: str,
        secret_ref: str | None = None,
    ) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO accounts (school_id, label, secret_ref, status)
                VALUES (%s, %s, %s, 'available')
                ON CONFLICT (school_id, label) DO UPDATE SET
                    secret_ref = COALESCE(EXCLUDED.secret_ref, accounts.secret_ref),
                    status = 'available',
                    updated_at = now()
                RETURNING *
                """,
                (school_id, label, secret_ref),
            )
            return cur.fetchone()

    def record_auth_session(
        self,
        account_id: int,
        status: str,
        token_expires_at=None,
        failure_reason: str | None = None,
        browser_profile_path: str | None = None,
    ) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth_sessions (
                    account_id,
                    status,
                    browser_profile_path,
                    token_expires_at,
                    last_validated_at,
                    failure_reason
                )
                VALUES (%s, %s, %s, %s, now(), %s)
                RETURNING *
                """,
                (
                    account_id,
                    status,
                    browser_profile_path,
                    token_expires_at,
                    failure_reason,
                ),
            )
            return cur.fetchone()

    def create_run(self, school_id: int, mode: str = "full", notes: str | None = None) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scrape_runs (school_id, mode, status, started_at, notes)
                VALUES (%s, %s, 'running', now(), %s)
                RETURNING *
                """,
                (school_id, mode, notes),
            )
            return cur.fetchone()

    def get_run(self, run_id: int) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM scrape_runs WHERE id = %s", (run_id,))
            return cur.fetchone()

    def latest_run_for_school(self, school_slug: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.*
                FROM scrape_runs r
                JOIN schools s ON s.id = r.school_id
                WHERE s.slug = %s
                ORDER BY r.id DESC
                LIMIT 1
                """,
                (school_slug,),
            )
            return cur.fetchone()

    def upsert_seed_user(
        self,
        school_id: int,
        external_user_id: str,
        listing_payload: dict,
        full_name: str | None = None,
        class_year: str | None = None,
        source_page: int | None = None,
    ) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alumni_seed (
                    school_id,
                    external_user_id,
                    listing_payload,
                    full_name,
                    class_year,
                    source_page
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (school_id, external_user_id) DO UPDATE SET
                    listing_payload = EXCLUDED.listing_payload,
                    full_name = COALESCE(EXCLUDED.full_name, alumni_seed.full_name),
                    class_year = COALESCE(EXCLUDED.class_year, alumni_seed.class_year),
                    source_page = COALESCE(EXCLUDED.source_page, alumni_seed.source_page),
                    last_seen_at = now()
                RETURNING *
                """,
                (
                    school_id,
                    external_user_id,
                    _jsonb(listing_payload),
                    full_name,
                    class_year,
                    source_page,
                ),
            )
            return cur.fetchone()

    def enqueue_profile_job(
        self,
        run_id: int,
        school_id: int,
        external_user_id: str,
    ) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO profile_jobs (run_id, school_id, external_user_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (run_id, external_user_id) DO UPDATE SET
                    updated_at = now()
                RETURNING *
                """,
                (run_id, school_id, external_user_id),
            )
            return cur.fetchone()

    def enqueue_jobs_from_seed(self, run_id: int, school_id: int) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO profile_jobs (run_id, school_id, external_user_id)
                SELECT %s, school_id, external_user_id
                FROM alumni_seed
                WHERE school_id = %s
                ON CONFLICT (run_id, external_user_id) DO NOTHING
                """,
                (run_id, school_id),
            )
            return cur.rowcount

    def claim_jobs(
        self,
        run_id: int,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 900,
    ) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                WITH claimed AS (
                    SELECT id
                    FROM profile_jobs
                    WHERE run_id = %s
                      AND status IN ('pending', 'retry')
                      AND (next_attempt_at IS NULL OR next_attempt_at <= now())
                      AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                    ORDER BY id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE profile_jobs
                SET
                    status = 'leased',
                    leased_by = %s,
                    lease_expires_at = now() + (%s::text || ' seconds')::interval,
                    attempt_count = attempt_count + 1,
                    updated_at = now()
                WHERE id IN (SELECT id FROM claimed)
                RETURNING *
                """,
                (run_id, limit, worker_id, lease_seconds),
            )
            return list(cur.fetchall())

    def get_seed_payload(self, school_id: int, external_user_id: str) -> dict | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT listing_payload
                FROM alumni_seed
                WHERE school_id = %s AND external_user_id = %s
                """,
                (school_id, external_user_id),
            )
            row = cur.fetchone()
            return row["listing_payload"] if row else None

    def upsert_profile_result(
        self,
        run_id: int,
        school_id: int,
        external_user_id: str,
        normalized_json: dict,
        profile_payload_ref: str | None,
        data_payload_ref: str | None,
        parser_version: str = "v1",
    ) -> dict:
        field_count = len(normalized_json)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO profile_results (
                    run_id,
                    school_id,
                    external_user_id,
                    profile_payload_ref,
                    data_payload_ref,
                    normalized_json,
                    field_count,
                    profile_fetched_at,
                    data_fetched_at,
                    parser_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s)
                ON CONFLICT (run_id, external_user_id) DO UPDATE SET
                    profile_payload_ref = EXCLUDED.profile_payload_ref,
                    data_payload_ref = EXCLUDED.data_payload_ref,
                    normalized_json = EXCLUDED.normalized_json,
                    field_count = EXCLUDED.field_count,
                    profile_fetched_at = EXCLUDED.profile_fetched_at,
                    data_fetched_at = EXCLUDED.data_fetched_at,
                    parser_version = EXCLUDED.parser_version,
                    updated_at = now()
                RETURNING *
                """,
                (
                    run_id,
                    school_id,
                    external_user_id,
                    profile_payload_ref,
                    data_payload_ref,
                    _jsonb(normalized_json),
                    field_count,
                    parser_version,
                ),
            )
            return cur.fetchone()

    def mark_job_complete(self, job_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE profile_jobs
                SET
                    status = 'complete',
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (job_id,),
            )

    def release_job(
        self,
        job_id: int,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE profile_jobs
                SET
                    status = 'pending',
                    leased_by = NULL,
                    lease_expires_at = NULL,
                    next_attempt_at = NULL,
                    last_error_code = COALESCE(%s, last_error_code),
                    last_error_message = COALESCE(%s, last_error_message),
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    error_code,
                    error_message[:1000] if error_message else None,
                    job_id,
                ),
            )

    def mark_job_retry(
        self,
        job_id: int,
        error_code: str,
        error_message: str,
        retry_delay_seconds: int = 300,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE profile_jobs
                SET
                    status = 'retry',
                    leased_by = NULL,
                    lease_expires_at = NULL,
                    next_attempt_at = now() + (%s::text || ' seconds')::interval,
                    last_error_code = %s,
                    last_error_message = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (retry_delay_seconds, error_code, error_message[:1000], job_id),
            )

    def heartbeat(
        self,
        worker_id: str,
        run_id: int | None,
        status: str,
        hostname: str | None = None,
        current_job_id: int | None = None,
        completed_count: int = 0,
        error_count: int = 0,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO worker_heartbeats (
                    worker_id,
                    run_id,
                    hostname,
                    status,
                    current_job_id,
                    completed_count,
                    error_count
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (worker_id) DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    hostname = EXCLUDED.hostname,
                    status = EXCLUDED.status,
                    current_job_id = EXCLUDED.current_job_id,
                    completed_count = EXCLUDED.completed_count,
                    error_count = EXCLUDED.error_count,
                    last_heartbeat_at = now(),
                    updated_at = now()
                """,
                (
                    worker_id,
                    run_id,
                    hostname,
                    status,
                    current_job_id,
                    completed_count,
                    error_count,
                ),
            )

    def summary(self, run_id: int) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, count(*) AS count
                FROM profile_jobs
                WHERE run_id = %s
                GROUP BY status
                ORDER BY status
                """,
                (run_id,),
            )
            jobs = {row["status"]: row["count"] for row in cur.fetchall()}
            cur.execute(
                """
                SELECT count(*) AS count
                FROM profile_results
                WHERE run_id = %s
                """,
                (run_id,),
            )
            results = cur.fetchone()["count"]
            cur.execute(
                """
                SELECT count(*) AS count
                FROM alumni_seed
                WHERE school_id = (SELECT school_id FROM scrape_runs WHERE id = %s)
                """,
                (run_id,),
            )
            seed_count = cur.fetchone()["count"]
            return {
                "run_id": run_id,
                "seed_count": seed_count,
                "result_count": results,
                "jobs": jobs,
            }

    def completed_results(self, run_id: int) -> Iterable[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT external_user_id, normalized_json
                FROM profile_results
                WHERE run_id = %s
                ORDER BY external_user_id
                """,
                (run_id,),
            )
            yield from cur

