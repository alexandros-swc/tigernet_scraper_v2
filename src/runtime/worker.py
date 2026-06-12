"""Queue-backed profile worker."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
import uuid

from config.settings import Settings
from src.auth import (
    AuthExpiredError,
    authenticate,
    get_access_token_expiry,
    load_cached_tokens,
    restore_browser_session,
)
from src.schools import get_adapter
from src.scraper import _api_fetch
from src.storage.db import connection, ensure_schema
from src.storage.raw_store import RawStore
from src.storage.repositories import ScrapeRepository

logger = logging.getLogger(__name__)


def work_school(
    school_slug: str,
    database_url: str | None = None,
    run_id: int | None = None,
    worker_id: str | None = None,
    batch_size: int = 5,
    max_jobs: int | None = None,
    lease_seconds: int = 900,
    headless: bool = True,
    raw_root: str = "output/raw",
    max_auth_refreshes: int = 12,
    auth_refresh_delay: float = 30.0,
) -> dict:
    """Process profile_jobs from the durable queue."""
    adapter = get_adapter(school_slug)
    settings = Settings(headless=headless)
    raw_store = RawStore(raw_root)
    worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

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
            label=os.getenv("TIGERNET_ACCOUNT_LABEL", "default"),
        )
        run = repo.get_run(run_id) if run_id else repo.latest_run_for_school(school_slug)
        if not run:
            raise RuntimeError("No scrape run found. Run seed first or pass --run-id.")
        if run["school_id"] != school["id"]:
            raise RuntimeError(f"Run {run['id']} does not belong to {school_slug}.")

        logger.info("Authenticating worker %s for run %s", worker_id, run["id"])
        tokens = authenticate(headless=headless)
        if not tokens:
            repo.record_auth_session(
                account_id=account["id"],
                status="auth_failed",
                failure_reason="authenticate returned no tokens",
            )
            raise RuntimeError("Authentication failed; worker cannot start.")
        repo.record_auth_session(
            account_id=account["id"],
            status="active",
            token_expires_at=get_access_token_expiry(tokens),
            browser_profile_path=tokens.get("browser_profile_path"),
        )

        from playwright.sync_api import sync_playwright

        completed = 0
        errors = 0
        auth_required = False
        auth_error = None
        auth_refreshes = 0
        stop_reason = "max_jobs_reached" if max_jobs is not None else "queue_empty"
        consecutive_empty_claims = 0
        my_user_id = tokens["my_user_id"]

        with sync_playwright() as p:
            browser, page = restore_browser_session(p, tokens, settings)
            try:
                repo.heartbeat(
                    worker_id=worker_id,
                    run_id=run["id"],
                    status="running",
                    hostname=socket.gethostname(),
                )

                while max_jobs is None or (completed + errors) < max_jobs:
                    remaining_capacity = (
                        None if max_jobs is None else max_jobs - completed - errors
                    )
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
                        if max_jobs is not None and (completed + errors) >= max_jobs:
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
                                page=page,
                                run_id=run["id"],
                                school_id=school["id"],
                                job=job,
                                my_user_id=my_user_id,
                            )
                            completed += 1
                            logger.info(
                                "Completed profile %s (%s total this worker)",
                                job["external_user_id"],
                                completed,
                            )
                        except AuthExpiredError as exc:
                            auth_error = str(exc)
                            logger.warning(
                                "Authentication failed; releasing leased jobs before refresh: %s",
                                auth_error,
                            )
                            for leased_job in jobs[job_index:]:
                                repo.release_job(
                                    job_id=leased_job["id"],
                                    error_code=exc.__class__.__name__,
                                    error_message=auth_error,
                                )

                            if auth_refreshes >= max_auth_refreshes:
                                auth_required = True
                                stop_reason = "auth_required"
                                logger.error(
                                    "Automatic auth refresh limit reached (%s).",
                                    max_auth_refreshes,
                                )
                                repo.record_auth_session(
                                    account_id=account["id"],
                                    status="auth_required",
                                    token_expires_at=get_access_token_expiry(tokens),
                                    failure_reason=auth_error,
                                    browser_profile_path=tokens.get("browser_profile_path"),
                                )
                                repo.heartbeat(
                                    worker_id=worker_id,
                                    run_id=run["id"],
                                    status="auth_required",
                                    hostname=socket.gethostname(),
                                    current_job_id=job["id"],
                                    completed_count=completed,
                                    error_count=errors,
                                )
                                break

                            auth_refreshes += 1
                            repo.record_auth_session(
                                account_id=account["id"],
                                status="refreshing",
                                token_expires_at=get_access_token_expiry(tokens),
                                failure_reason=auth_error,
                                browser_profile_path=tokens.get("browser_profile_path"),
                            )
                            repo.heartbeat(
                                worker_id=worker_id,
                                run_id=run["id"],
                                status="refreshing_auth",
                                hostname=socket.gethostname(),
                                current_job_id=job["id"],
                                completed_count=completed,
                                error_count=errors,
                            )
                            logger.info(
                                "Attempting automatic TigerNet auth refresh %s/%s.",
                                auth_refreshes,
                                max_auth_refreshes,
                            )
                            if auth_refresh_delay > 0:
                                time.sleep(auth_refresh_delay)
                            _close_browser(browser)
                            try:
                                refreshed_tokens = _refresh_tokens_in_subprocess(
                                    headless=headless,
                                )
                                if not refreshed_tokens:
                                    raise RuntimeError(
                                        "automatic auth refresh returned no tokens"
                                    )
                                tokens = refreshed_tokens
                                my_user_id = tokens["my_user_id"]
                                browser, page = restore_browser_session(
                                    p,
                                    tokens,
                                    settings,
                                )
                            except Exception as refresh_exc:
                                auth_required = True
                                stop_reason = "auth_required"
                                auth_error = f"automatic auth refresh failed: {refresh_exc}"
                                logger.error(auth_error)
                                repo.record_auth_session(
                                    account_id=account["id"],
                                    status="auth_required",
                                    token_expires_at=None,
                                    failure_reason=auth_error,
                                    browser_profile_path=tokens.get("browser_profile_path"),
                                )
                                repo.heartbeat(
                                    worker_id=worker_id,
                                    run_id=run["id"],
                                    status="auth_required",
                                    hostname=socket.gethostname(),
                                    current_job_id=job["id"],
                                    completed_count=completed,
                                    error_count=errors,
                                )
                                break

                            repo.record_auth_session(
                                account_id=account["id"],
                                status="active",
                                token_expires_at=get_access_token_expiry(tokens),
                                browser_profile_path=tokens.get("browser_profile_path"),
                            )
                            auth_error = None
                            consecutive_empty_claims = 0
                            repo.heartbeat(
                                worker_id=worker_id,
                                run_id=run["id"],
                                status="running",
                                hostname=socket.gethostname(),
                                completed_count=completed,
                                error_count=errors,
                            )
                            logger.info(
                                "Automatic auth refresh succeeded; resuming queue work."
                            )
                            break
                        except Exception as exc:
                            errors += 1
                            logger.exception(
                                "Profile %s failed",
                                job["external_user_id"],
                            )
                            repo.mark_job_retry(
                                job_id=job["id"],
                                error_code=exc.__class__.__name__,
                                error_message=str(exc),
                            )
                        time.sleep(settings.request_delay)
                    if auth_required:
                        break
            finally:
                repo.heartbeat(
                    worker_id=worker_id,
                    run_id=run["id"],
                    status="auth_required" if auth_required else "stopped",
                    hostname=socket.gethostname(),
                    completed_count=completed,
                    error_count=errors,
                )
                _close_browser(browser)

    return {
        "run_id": run["id"],
        "worker_id": worker_id,
        "completed": completed,
        "errors": errors,
        "auth_required": auth_required,
        "auth_refreshes": auth_refreshes,
        "stop_reason": stop_reason,
        "auth_error": auth_error,
    }


def _close_browser(browser) -> None:
    try:
        browser.close()
    except Exception:
        pass


def _refresh_tokens_in_subprocess(headless: bool) -> dict | None:
    """Refresh auth without nesting Playwright's sync driver in this process."""
    script = (
        "from src.utils import setup_logging; "
        "from src.auth import refresh_tokens; "
        "setup_logging(); "
        f"tokens = refresh_tokens(headless={headless!r}); "
        "raise SystemExit(0 if tokens else 1)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        check=False,
    )
    if result.returncode != 0:
        return None
    return load_cached_tokens()


def _process_job(
    repo: ScrapeRepository,
    raw_store: RawStore,
    adapter,
    page,
    run_id: int,
    school_id: int,
    job: dict,
    my_user_id: str,
) -> None:
    external_user_id = job["external_user_id"]
    listing_payload = repo.get_seed_payload(school_id, external_user_id)
    if not listing_payload:
        raise RuntimeError(f"Missing seed payload for {external_user_id}")

    full_raw = _api_fetch(
        page,
        adapter.build_full_profile_url(my_user_id, external_user_id),
    )
    data_raw = _api_fetch(
        page,
        adapter.build_profile_data_url(external_user_id),
    )

    if not full_raw and not data_raw:
        if not _auth_probe_succeeded(adapter, page):
            raise AuthExpiredError(
                "TigerNet profile endpoints and directory auth probe failed"
            )
        raise RuntimeError("Both profile endpoints failed or returned no data.")

    full_profile = None
    if isinstance(full_raw, dict):
        full_profile = full_raw.get("user", full_raw)

    profile_ref = None
    data_ref = None
    if full_raw:
        profile_ref = raw_store.put_json(
            f"{adapter.slug}/run_{run_id}/profiles/{external_user_id}/full_profile.json.gz",
            full_raw,
        )
    if data_raw:
        data_ref = raw_store.put_json(
            f"{adapter.slug}/run_{run_id}/profiles/{external_user_id}/data.json.gz",
            data_raw,
        )

    normalized = adapter.normalize_profile(
        listing_payload=listing_payload,
        full_profile_payload=full_profile,
        data_payload=data_raw,
    )
    repo.upsert_profile_result(
        run_id=run_id,
        school_id=school_id,
        external_user_id=external_user_id,
        normalized_json=normalized,
        profile_payload_ref=profile_ref,
        data_payload_ref=data_ref,
    )
    repo.mark_job_complete(job["id"])


def _auth_probe_succeeded(adapter, page) -> bool:
    probe = _api_fetch(page, adapter.build_listing_url(page=1, per_page=1), max_retries=1)
    if not isinstance(probe, dict):
        return False
    if adapter.extract_total_users(probe) is not None:
        return True
    return bool(adapter.extract_listing_users(probe))

