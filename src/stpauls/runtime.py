"""DB-backed runtime commands for the isolated St. Paul's scraper."""

from __future__ import annotations

import csv
import logging
import math
import os
import socket
import time
import uuid

from src.runtime.export_db import export_results_to_csv
from src.runtime.status import get_status
from src.stpauls.adapter import StPaulsAdapter
from src.stpauls.auth import (
    StPaulsAuthExpiredError,
    authenticate,
    get_access_token_expiry,
    inspect_token_cache,
    load_cached_tokens,
)
from src.stpauls.client import StPaulsApiClient
from src.storage.db import connection, ensure_schema
from src.storage.raw_store import RawStore
from src.storage.repositories import ScrapeRepository

logger = logging.getLogger(__name__)


def auth_check(
    headless: bool = True,
    login_if_needed: bool = False,
    api_check: bool = True,
) -> dict:
    result = {
        "cache": inspect_token_cache(),
        "login_attempted": False,
    }
    tokens = load_cached_tokens()
    if not tokens and login_if_needed:
        result["login_attempted"] = True
        tokens = authenticate(headless=headless)
        result["cache_after_login"] = inspect_token_cache()

    if not api_check:
        result["api_check"] = {"status": "skipped"}
        return result

    if not tokens:
        result["api_check"] = {
            "status": "skipped",
            "reason": "no valid cached token; pass --login-if-needed to perform login",
        }
        return result

    try:
        payload = StPaulsApiClient(tokens).check()
        result["api_check"] = {
            "status": "ok",
            "keys": sorted(payload.keys())[:20] if isinstance(payload, dict) else [],
        }
    except StPaulsAuthExpiredError as exc:
        result["api_check"] = {
            "status": "auth_required",
            "reason": str(exc),
        }
    return result


def seed(
    database_url: str | None = None,
    run_id: int | None = None,
    per_page: int | None = None,
    max_pages: int | None = None,
    headless: bool = False,
    raw_root: str = "output/raw",
) -> dict:
    adapter = StPaulsAdapter()
    per_page = per_page or adapter.default_per_page
    raw_store = RawStore(raw_root)
    tokens = authenticate(headless=headless)
    if not tokens:
        raise RuntimeError("St. Paul's authentication failed; cannot seed jobs.")
    client = StPaulsApiClient(tokens, adapter)

    with connection(database_url) as conn:
        ensure_schema(conn)
        repo = ScrapeRepository(conn)
        school = repo.ensure_school(
            slug=adapter.slug,
            base_url=adapter.base_url,
            platform=adapter.platform,
        )
        run = repo.get_run(run_id) if run_id else None
        if run is None:
            run = repo.create_run(
                school_id=school["id"],
                mode="seed",
                notes="Seed run for St. Paul's",
            )

        first_payload = client.search_directory(page=1, per_page=per_page)
        total_users = adapter.extract_total_users(first_payload)
        if total_users is None and max_pages is None:
            raise RuntimeError("Could not determine total pages; pass --max-pages.")
        total_pages = max_pages if total_users is None else math.ceil(total_users / per_page)
        if max_pages is not None:
            total_pages = min(total_pages, max_pages)

        total_seeded = 0
        total_jobs = 0
        for page_number in range(1, total_pages + 1):
            payload = (
                first_payload
                if page_number == 1
                else client.search_directory(page=page_number, per_page=per_page)
            )
            raw_store.put_json(
                f"{adapter.slug}/run_{run['id']}/listing/page_{page_number}.json.gz",
                payload,
            )
            users = adapter.extract_listing_users(payload)
            if not users:
                logger.warning("St. Paul's listing page %s returned no users", page_number)
                continue
            for user in users:
                external_user_id = adapter.external_user_id(user)
                flat_hint = adapter.normalize_profile(user, None)
                repo.upsert_seed_user(
                    school_id=school["id"],
                    external_user_id=external_user_id,
                    listing_payload=user,
                    full_name=flat_hint.get("full_name"),
                    class_year=flat_hint.get("class_year"),
                    source_page=page_number,
                )
                repo.enqueue_profile_job(
                    run_id=run["id"],
                    school_id=school["id"],
                    external_user_id=external_user_id,
                )
                total_seeded += 1
                total_jobs += 1
            logger.info(
                "Seeded St. Paul's page %s/%s: %s users",
                page_number,
                total_pages,
                len(users),
            )
            time.sleep(0.5)

    return {
        "run_id": run["id"],
        "school": adapter.slug,
        "total_users": total_users,
        "pages_seen": total_pages,
        "seeded_rows_seen": total_seeded,
        "jobs_seen": total_jobs,
    }


def work(
    database_url: str | None = None,
    run_id: int | None = None,
    worker_id: str | None = None,
    batch_size: int = 5,
    max_jobs: int | None = None,
    lease_seconds: int = 900,
    headless: bool = True,
    raw_root: str = "output/raw",
    request_delay: float = 0.5,
) -> dict:
    adapter = StPaulsAdapter()
    raw_store = RawStore(raw_root)
    worker_id = worker_id or f"{socket.gethostname()}-stpauls-{uuid.uuid4().hex[:8]}"

    with connection(database_url) as conn:
        ensure_schema(conn)
        repo = ScrapeRepository(conn)
        school = repo.ensure_school(
            slug=adapter.slug,
            base_url=adapter.base_url,
            platform=adapter.platform,
        )
        account = repo.ensure_account(
            school_id=school["id"],
            label=os.getenv("STPAULS_ACCOUNT_LABEL", "default"),
        )
        run = repo.get_run(run_id) if run_id else repo.latest_run_for_school(adapter.slug)
        if not run:
            raise RuntimeError("No St. Paul's scrape run found. Run seed first.")
        if run["school_id"] != school["id"]:
            raise RuntimeError(f"Run {run['id']} does not belong to St. Paul's.")

        tokens = authenticate(headless=headless)
        if not tokens:
            repo.record_auth_session(
                account_id=account["id"],
                status="auth_failed",
                failure_reason="authenticate returned no token",
            )
            raise RuntimeError("St. Paul's authentication failed; worker cannot start.")
        repo.record_auth_session(
            account_id=account["id"],
            status="active",
            token_expires_at=get_access_token_expiry(tokens),
            browser_profile_path=tokens.get("browser_profile_path"),
        )
        client = StPaulsApiClient(tokens, adapter)

        completed = 0
        errors = 0
        auth_required = False
        auth_error = None
        stop_reason = "max_jobs_reached" if max_jobs is not None else "queue_empty"
        consecutive_empty_claims = 0

        repo.heartbeat(
            worker_id=worker_id,
            run_id=run["id"],
            status="running",
            hostname=socket.gethostname(),
        )
        while max_jobs is None or completed < max_jobs:
            remaining_capacity = None if max_jobs is None else max_jobs - completed
            claim_limit = batch_size if remaining_capacity is None else min(batch_size, remaining_capacity)
            jobs = repo.claim_jobs(
                run_id=run["id"],
                worker_id=worker_id,
                limit=claim_limit,
                lease_seconds=lease_seconds,
            )
            if not jobs:
                consecutive_empty_claims += 1
                repo.heartbeat(
                    worker_id=worker_id,
                    run_id=run["id"],
                    status="idle",
                    hostname=socket.gethostname(),
                    completed_count=completed,
                    error_count=errors,
                )
                if consecutive_empty_claims >= 2:
                    stop_reason = "queue_empty"
                    break
                time.sleep(5)
                continue

            consecutive_empty_claims = 0
            for job_index, job in enumerate(jobs):
                if max_jobs is not None and completed >= max_jobs:
                    break
                try:
                    repo.heartbeat(
                        worker_id=worker_id,
                        run_id=run["id"],
                        status="working",
                        hostname=socket.gethostname(),
                        current_job_id=job["id"],
                        completed_count=completed,
                        error_count=errors,
                    )
                    _process_job(
                        repo=repo,
                        raw_store=raw_store,
                        adapter=adapter,
                        client=client,
                        run_id=run["id"],
                        school_id=school["id"],
                        job=job,
                    )
                    completed += 1
                    logger.info(
                        "Completed St. Paul's profile %s (%s total this worker)",
                        job["external_user_id"],
                        completed,
                    )
                except StPaulsAuthExpiredError as exc:
                    auth_required = True
                    auth_error = str(exc)
                    stop_reason = "auth_required"
                    for leased_job in jobs[job_index:]:
                        repo.release_job(
                            job_id=leased_job["id"],
                            error_code=exc.__class__.__name__,
                            error_message=auth_error,
                        )
                    repo.record_auth_session(
                        account_id=account["id"],
                        status="auth_required",
                        token_expires_at=get_access_token_expiry(tokens),
                        failure_reason=auth_error,
                        browser_profile_path=tokens.get("browser_profile_path"),
                    )
                    break
                except Exception as exc:
                    errors += 1
                    logger.exception("St. Paul's profile %s failed", job["external_user_id"])
                    repo.mark_job_retry(
                        job_id=job["id"],
                        error_code=exc.__class__.__name__,
                        error_message=str(exc),
                    )
                time.sleep(request_delay)
            if auth_required:
                break

        repo.heartbeat(
            worker_id=worker_id,
            run_id=run["id"],
            status="auth_required" if auth_required else "stopped",
            hostname=socket.gethostname(),
            completed_count=completed,
            error_count=errors,
        )

    return {
        "run_id": run["id"],
        "worker_id": worker_id,
        "completed": completed,
        "errors": errors,
        "auth_required": auth_required,
        "stop_reason": stop_reason,
        "auth_error": auth_error,
    }


def smoke(
    count: int = 3,
    headless: bool = True,
    output_path: str = "output/stpauls/smoke_profiles.csv",
) -> dict:
    adapter = StPaulsAdapter()
    tokens = authenticate(headless=headless)
    if not tokens:
        raise RuntimeError("St. Paul's authentication failed.")
    client = StPaulsApiClient(tokens, adapter)

    listing = client.search_directory(page=1, per_page=max(count, 1))
    users = adapter.extract_listing_users(listing)[:count]
    rows = []
    for user in users:
        external_id = adapter.external_user_id(user)
        profile = client.fetch_profile(external_id)
        rows.append(adapter.normalize_profile(user, profile))
        time.sleep(0.5)

    _write_csv(output_path, rows)
    return {
        "rows": len(rows),
        "output_path": output_path,
        "external_ids": [row.get("external_id") for row in rows],
    }


def status(database_url: str | None = None, run_id: int | None = None) -> dict:
    return get_status(
        school_slug=StPaulsAdapter.slug,
        database_url=database_url,
        run_id=run_id,
    )


def export_db(
    output_path: str = "output/stpauls/stpauls_alumni_db.csv",
    database_url: str | None = None,
    run_id: int | None = None,
) -> dict:
    return export_results_to_csv(
        output_path=output_path,
        school_slug=StPaulsAdapter.slug,
        database_url=database_url,
        run_id=run_id,
    )


def _process_job(
    repo: ScrapeRepository,
    raw_store: RawStore,
    adapter: StPaulsAdapter,
    client: StPaulsApiClient,
    run_id: int,
    school_id: int,
    job: dict,
) -> None:
    external_user_id = job["external_user_id"]
    listing_payload = repo.get_seed_payload(school_id, external_user_id)
    if not listing_payload:
        raise RuntimeError(f"Missing St. Paul's seed payload for {external_user_id}")

    full_raw = client.fetch_profile(external_user_id)
    profile_ref = raw_store.put_json(
        f"{adapter.slug}/run_{run_id}/profiles/{external_user_id}/full_profile.json.gz",
        full_raw,
    )
    normalized = adapter.normalize_profile(
        listing_payload=listing_payload,
        full_profile_payload=full_raw,
    )
    repo.upsert_profile_result(
        run_id=run_id,
        school_id=school_id,
        external_user_id=external_user_id,
        normalized_json=normalized,
        profile_payload_ref=profile_ref,
        data_payload_ref=None,
    )
    repo.mark_job_complete(job["id"])


def _write_csv(output_path: str, rows: list[dict]) -> None:
    if not rows:
        raise RuntimeError("No St. Paul's rows to export.")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    priority = [
        "id",
        "external_id",
        "full_name",
        "firstname",
        "lastname",
        "maidenname",
        "class_year",
        "email",
        "phone",
        "current_job",
        "company_name",
        "city",
        "state",
        "country",
        "linkedin_profile_url",
        "profile_url",
    ]
    ordered = [field for field in priority if field in fieldnames]
    ordered.extend(field for field in fieldnames if field not in set(ordered))
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ordered, restval="", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
