"""DB-backed runtime commands for the isolated Columbia scraper."""

from __future__ import annotations

import csv
import hashlib
import logging
import os
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlparse

from src.columbia.api import (
    coveo_search,
    extract_coveo_rows,
    extract_linkedin_from_salesforce_fields,
    fetch_salesforce_user_fields,
    total_count,
)
from src.columbia.adapter import ColumbiaAdapter
from src.columbia.auth import (
    ColumbiaAuthRequiredError,
    authenticate,
    get_access_token_expiry,
    get_start_url,
    inspect_token_cache,
    load_cached_tokens,
    refresh_tokens,
    restore_browser_session,
)
from src.columbia.client import ColumbiaAuthExpiredError, api_fetch
from src.runtime.export_db import export_results_to_csv
from src.runtime.status import get_status
from src.storage.db import connection, ensure_schema
from src.storage.raw_store import RawStore
from src.storage.repositories import ScrapeRepository

logger = logging.getLogger(__name__)


def auth_check(headless: bool = True, login_if_needed: bool = False, api_check: bool = True) -> dict:
    result = {"cache": inspect_token_cache(), "login_attempted": False}
    tokens = authenticate(headless=headless) if login_if_needed else load_cached_tokens()
    if login_if_needed:
        result["login_attempted"] = True
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

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page = _restore_or_refresh_session(p, tokens, headless=headless)
        try:
            payload = coveo_search(page, first_result=0, number_of_results=1)
            rows = extract_coveo_rows(payload, source_page=1, per_page=1) if payload else []
            extraction_method = "coveo_api" if rows else "ui"
            if not rows:
                rows = _extract_visible_search_results(page, count=1)
            result["api_check"] = {
                "status": "ok" if rows else "failed",
                "current_url": page.url,
                "visible_results": len(rows),
                "first_result": rows[0] if rows else None,
                "extraction_method": extraction_method,
            }
        finally:
            browser.close()
    return result


def seed(
    database_url: str | None = None,
    run_id: int | None = None,
    per_page: int | None = None,
    max_pages: int | None = None,
    headless: bool = False,
    raw_root: str = "output/raw",
) -> dict:
    tokens = authenticate(headless=headless)
    if not tokens:
        raise RuntimeError("Columbia authentication failed; cannot seed jobs.")
    adapter = ColumbiaAdapter(tokens.get("base_url"))
    raw_store = RawStore(raw_root)

    from playwright.sync_api import sync_playwright

    with connection(database_url) as conn:
        ensure_schema(conn)
        repo = ScrapeRepository(conn)
        school = repo.ensure_school(adapter.slug, adapter.base_url, adapter.platform)
        run = repo.get_run(run_id) if run_id else None
        if run is None:
            run = repo.create_run(school["id"], mode="seed", notes="Seed run for Columbia")

        total_seeded = 0
        total_jobs = 0
        with sync_playwright() as p:
            browser, page = _restore_or_refresh_session(p, tokens, headless=headless)
            try:
                page_size = per_page or 100
                try:
                    pages_seen, total_seeded, total_jobs = _seed_from_coveo_api(
                        repo=repo,
                        raw_store=raw_store,
                        adapter=adapter,
                        page=page,
                        run=run,
                        school=school,
                        per_page=page_size,
                        max_pages=max_pages,
                    )
                except Exception as exc:
                    logger.warning(
                        "Columbia Coveo API seed failed; falling back to rendered UI extraction: %s",
                        exc,
                    )
                    pages_seen, total_seeded, total_jobs = _seed_from_rendered_ui(
                        repo=repo,
                        raw_store=raw_store,
                        adapter=adapter,
                        page=page,
                        run=run,
                        school=school,
                        per_page=page_size,
                        max_pages=max_pages,
                    )
            finally:
                browser.close()

    return {
        "run_id": run["id"],
        "school": adapter.slug,
        "pages_seen": pages_seen,
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
    max_auth_refreshes: int = 12,
    auth_refresh_delay: float = 30.0,
    simulate_auth_expiry_after_jobs: int | None = None,
) -> dict:
    tokens = authenticate(headless=headless)
    if not tokens:
        raise RuntimeError("Columbia authentication failed; worker cannot start.")
    adapter = ColumbiaAdapter(tokens.get("base_url"))
    raw_store = RawStore(raw_root)
    worker_id = worker_id or f"{socket.gethostname()}-columbia-{uuid.uuid4().hex[:8]}"

    from playwright.sync_api import sync_playwright

    with connection(database_url) as conn:
        ensure_schema(conn)
        repo = ScrapeRepository(conn)
        school = repo.ensure_school(adapter.slug, adapter.base_url, adapter.platform)
        account = repo.ensure_account(school["id"], os.getenv("COLUMBIA_ACCOUNT_LABEL", "default"))
        run = repo.get_run(run_id) if run_id else repo.latest_run_for_school(adapter.slug)
        if not run:
            raise RuntimeError("No Columbia scrape run found. Run seed first.")
        if run["school_id"] != school["id"]:
            raise RuntimeError(f"Run {run['id']} does not belong to Columbia.")

        repo.record_auth_session(
            account_id=account["id"],
            status="active",
            token_expires_at=get_access_token_expiry(tokens),
            browser_profile_path=tokens.get("browser_profile_path"),
        )

        completed = 0
        errors = 0
        auth_required = False
        auth_error = None
        auth_refreshes = 0
        stop_reason = "max_jobs_reached" if max_jobs is not None else "queue_empty"
        consecutive_empty_claims = 0

        with sync_playwright() as p:
            browser, page = _restore_or_refresh_session(p, tokens, headless=headless)
            try:
                repo.heartbeat(worker_id, run["id"], "running", socket.gethostname())
                while max_jobs is None or (completed + errors) < max_jobs:
                    remaining = None if max_jobs is None else max_jobs - completed - errors
                    claim_limit = batch_size if remaining is None else min(batch_size, remaining)
                    jobs = repo.claim_jobs(run["id"], worker_id, claim_limit, lease_seconds)
                    if not jobs:
                        consecutive_empty_claims += 1
                        repo.heartbeat(worker_id, run["id"], "idle", socket.gethostname(), completed_count=completed, error_count=errors)
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
                                worker_id,
                                run["id"],
                                "working",
                                socket.gethostname(),
                                current_job_id=job["id"],
                                completed_count=completed,
                                error_count=errors,
                            )
                            _process_job(repo, raw_store, adapter, page, run["id"], school["id"], job)
                            completed += 1
                            logger.info("Completed Columbia profile %s (%s total)", job["external_user_id"], completed)
                            if (
                                simulate_auth_expiry_after_jobs is not None
                                and auth_refreshes == 0
                                and completed >= simulate_auth_expiry_after_jobs
                            ):
                                _close_browser(browser)
                                raise ColumbiaAuthRequiredError(
                                    "Simulated Columbia auth expiry for refresh-path test"
                                )
                        except (ColumbiaAuthExpiredError, ColumbiaAuthRequiredError) as exc:
                            auth_error = str(exc)
                            for leased_job in jobs[job_index:]:
                                repo.release_job(leased_job["id"], exc.__class__.__name__, auth_error)
                            if auth_refreshes >= max_auth_refreshes:
                                auth_required = True
                                stop_reason = "auth_required"
                                break
                            auth_refreshes += 1
                            repo.record_auth_session(account["id"], "refreshing", failure_reason=auth_error)
                            if auth_refresh_delay > 0:
                                time.sleep(auth_refresh_delay)
                            _close_browser(browser)
                            tokens = _refresh_tokens_in_subprocess(headless=headless)
                            if not tokens:
                                auth_required = True
                                stop_reason = "auth_required"
                                auth_error = "automatic Columbia auth refresh failed"
                                break
                            adapter = ColumbiaAdapter(tokens.get("base_url"))
                            browser, page = restore_browser_session(p, tokens)
                            repo.record_auth_session(
                                account["id"],
                                "active",
                                token_expires_at=get_access_token_expiry(tokens),
                                browser_profile_path=tokens.get("browser_profile_path"),
                            )
                            break
                        except Exception as exc:
                            errors += 1
                            logger.exception("Columbia profile %s failed", job["external_user_id"])
                            repo.mark_job_retry(job["id"], exc.__class__.__name__, str(exc))
                        time.sleep(request_delay)
                    if auth_required:
                        break
            finally:
                repo.heartbeat(
                    worker_id,
                    run["id"],
                    "auth_required" if auth_required else "stopped",
                    socket.gethostname(),
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


def smoke(count: int = 3, headless: bool = True, output_path: str = "output/columbia/smoke_profiles.csv") -> dict:
    tokens = authenticate(headless=headless)
    if not tokens:
        raise RuntimeError("Columbia authentication failed.")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page = _restore_or_refresh_session(p, tokens, headless=headless)
        try:
            payload = coveo_search(page, first_result=0, number_of_results=count)
            rows = extract_coveo_rows(payload, source_page=1, per_page=count) if payload else []
            if not rows:
                rows = _extract_visible_search_results(page, count=count)
            _enrich_rows_from_profiles(page, rows)
        finally:
            browser.close()

    _write_csv(output_path, rows)
    return {
        "rows": len(rows),
        "output_path": output_path,
        "names": [row.get("full_name") for row in rows],
    }


def _restore_or_refresh_session(playwright_instance, tokens: dict, headless: bool = True):
    try:
        return restore_browser_session(playwright_instance, tokens)
    except ColumbiaAuthRequiredError:
        logger.info("Columbia session restore failed. Refreshing auth via persistent browser profile...")
        refreshed_tokens = _refresh_tokens_in_subprocess(headless=headless)
        if not refreshed_tokens:
            raise RuntimeError("Columbia auth refresh failed; interactive login is required.")
        return restore_browser_session(playwright_instance, refreshed_tokens)


def status(database_url: str | None = None, run_id: int | None = None) -> dict:
    return get_status(ColumbiaAdapter.slug, database_url=database_url, run_id=run_id)


def export_db(
    output_path: str = "output/columbia/columbia_alumni_db.csv",
    database_url: str | None = None,
    run_id: int | None = None,
) -> dict:
    return export_results_to_csv(output_path, ColumbiaAdapter.slug, database_url=database_url, run_id=run_id)


def _seed_from_coveo_api(
    repo: ScrapeRepository,
    raw_store: RawStore,
    adapter: ColumbiaAdapter,
    page,
    run: dict,
    school: dict,
    per_page: int,
    max_pages: int | None,
) -> tuple[int, int, int]:
    page_size = max(1, min(per_page, 100))
    page_number = 1
    pages_seen = 0
    total_seeded = 0
    total_jobs = 0
    total_available: int | None = None

    while True:
        first_result = (page_number - 1) * page_size
        payload = coveo_search(
            page,
            first_result=first_result,
            number_of_results=page_size,
        )
        if not payload:
            if pages_seen == 0:
                raise RuntimeError("Columbia Coveo API returned no payload during seed")
            break

        rows = extract_coveo_rows(payload, source_page=page_number, per_page=page_size)
        total_available = total_available if total_available is not None else total_count(payload)
        if not rows:
            if pages_seen == 0:
                raise RuntimeError("Columbia Coveo API returned no alumni rows during seed")
            break

        raw_store.put_json(
            f"{adapter.slug}/run_{run['id']}/listing/page_{page_number}.json.gz",
            {
                "page": page_number,
                "first_result": first_result,
                "per_page": page_size,
                "total_available": total_available,
                "extraction_method": "coveo_api",
                "payload": payload,
                "rows": rows,
            },
        )
        for row in rows:
            external_user_id = row.get("external_id") or _external_id_for_row(row)
            row["external_id"] = str(external_user_id)
            repo.upsert_seed_user(
                school_id=school["id"],
                external_user_id=str(external_user_id),
                listing_payload=row,
                full_name=row.get("full_name"),
                class_year=row.get("class_tag"),
                source_page=page_number,
            )
            repo.enqueue_profile_job(run["id"], school["id"], str(external_user_id))
            total_seeded += 1
            total_jobs += 1

        pages_seen += 1
        logger.info(
            "Seeded Columbia page %s from Coveo API: %s results",
            page_number,
            len(rows),
        )
        if max_pages is not None and pages_seen >= max_pages:
            break
        if total_available is not None and first_result + len(rows) >= total_available:
            break
        page_number += 1
        time.sleep(0.2)

    return pages_seen, total_seeded, total_jobs


def _seed_from_rendered_ui(
    repo: ScrapeRepository,
    raw_store: RawStore,
    adapter: ColumbiaAdapter,
    page,
    run: dict,
    school: dict,
    per_page: int,
    max_pages: int | None,
) -> tuple[int, int, int]:
    page.goto(get_start_url(), wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    if per_page:
        _set_results_per_page(page, per_page)

    page_number = 1
    pages_seen = 0
    total_seeded = 0
    total_jobs = 0
    while True:
        rows = _extract_visible_search_results(page, count=per_page or 100)
        if not rows:
            raise ColumbiaAuthExpiredError(
                "Columbia search results were not visible during seed"
            )
        for row in rows:
            row["source_page"] = page_number
            row["result_page_url"] = page.url
            row["extraction_method"] = "rendered_ui"
            row["external_id"] = _external_id_for_row(row)

        raw_store.put_json(
            f"{adapter.slug}/run_{run['id']}/listing/page_{page_number}.json.gz",
            {
                "page": page_number,
                "url": page.url,
                "extraction_method": "rendered_ui",
                "rows": rows,
            },
        )
        for row in rows:
            external_user_id = row["external_id"]
            repo.upsert_seed_user(
                school_id=school["id"],
                external_user_id=external_user_id,
                listing_payload=row,
                full_name=row.get("full_name"),
                class_year=row.get("class_tag"),
                source_page=page_number,
            )
            repo.enqueue_profile_job(run["id"], school["id"], external_user_id)
            total_seeded += 1
            total_jobs += 1

        pages_seen += 1
        logger.info(
            "Seeded Columbia page %s from rendered UI: %s visible results",
            page_number,
            len(rows),
        )
        if max_pages is not None and pages_seen >= max_pages:
            break
        if not _go_to_next_results_page(page, page_number):
            break
        page_number += 1
        time.sleep(0.5)

    return pages_seen, total_seeded, total_jobs


def _process_job(
    repo: ScrapeRepository,
    raw_store: RawStore,
    adapter: ColumbiaAdapter,
    page,
    run_id: int,
    school_id: int,
    job: dict,
) -> None:
    external_user_id = job["external_user_id"]
    listing_payload = repo.get_seed_payload(school_id, external_user_id)
    if not listing_payload:
        raise RuntimeError(f"Missing Columbia seed payload for {external_user_id}")

    if listing_payload.get("extraction_method") == "coveo_api":
        _process_coveo_job(repo, raw_store, adapter, page, run_id, school_id, job, listing_payload)
        return

    target_page = int(listing_payload.get("source_page") or 1)
    _go_to_results_page(page, target_page)
    if not _open_profile_for_row(page, listing_payload):
        row = dict(listing_payload)
        row["profile_open_error"] = "could_not_open_profile"
        profile_ref = raw_store.put_json(
            f"{adapter.slug}/run_{run_id}/profiles/{external_user_id}/listing_only.json.gz",
            {"listing": listing_payload, "profile": row},
        )
        repo.upsert_profile_result(
            run_id=run_id,
            school_id=school_id,
            external_user_id=external_user_id,
            normalized_json=row,
            profile_payload_ref=profile_ref,
            data_payload_ref=None,
            parser_version="salesforce_v1",
        )
        repo.mark_job_complete(job["id"])
        return
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    time.sleep(1)

    row = dict(listing_payload)
    details = _extract_profile_details(page)
    if not details.get("linkedin_profile_url"):
        clicked_linkedin = _click_profile_linkedin_button(
            page,
            row.get("full_name", ""),
        )
        if clicked_linkedin:
            details["linkedin_profile_url"] = clicked_linkedin
    row.update({key: value for key, value in details.items() if value})

    profile_ref = raw_store.put_json(
        f"{adapter.slug}/run_{run_id}/profiles/{external_user_id}/profile.json.gz",
        {"listing": listing_payload, "profile": row, "url": page.url},
    )

    repo.upsert_profile_result(
        run_id=run_id,
        school_id=school_id,
        external_user_id=external_user_id,
        normalized_json=row,
        profile_payload_ref=profile_ref,
        data_payload_ref=None,
        parser_version="salesforce_v1",
    )
    repo.mark_job_complete(job["id"])
    _return_to_results(page, listing_payload.get("result_page_url") or page.url)


def _process_coveo_job(
    repo: ScrapeRepository,
    raw_store: RawStore,
    adapter: ColumbiaAdapter,
    page,
    run_id: int,
    school_id: int,
    job: dict,
    listing_payload: dict,
) -> None:
    external_user_id = job["external_user_id"]
    row = dict(listing_payload)
    row["profile_extraction_method"] = "coveo_api"
    salesforce_payload = None

    if not row.get("linkedin_profile_url") and row.get("salesforce_user_id"):
        salesforce_payload = fetch_salesforce_user_fields(page, row["salesforce_user_id"])
        api_linkedin_fields = extract_linkedin_from_salesforce_fields(salesforce_payload)
        if api_linkedin_fields.get("linkedin_profile_url"):
            api_linkedin_fields["linkedin_extraction_method"] = "salesforce_ui_api"
        row.update(api_linkedin_fields)

    if not row.get("linkedin_profile_url"):
        linkedin_url = _try_linkedin_from_profile_ui(page, row)
        if linkedin_url:
            row["linkedin_profile_url"] = linkedin_url
            row["linkedin_extraction_method"] = "profile_ui_click"

    profile_ref = raw_store.put_json(
        f"{adapter.slug}/run_{run_id}/profiles/{external_user_id}/profile.json.gz",
        {
            "listing": listing_payload,
            "profile": row,
            "extraction_method": "coveo_api",
            "salesforce_user_fields": salesforce_payload,
        },
    )

    repo.upsert_profile_result(
        run_id=run_id,
        school_id=school_id,
        external_user_id=external_user_id,
        normalized_json=row,
        profile_payload_ref=profile_ref,
        data_payload_ref=None,
        parser_version="coveo_api_v1",
    )
    repo.mark_job_complete(job["id"])


def _try_linkedin_from_profile_ui(page, row: dict) -> str:
    """Fallback for LinkedIn URLs that are only exposed by the profile icon."""
    name = row.get("full_name", "")
    if not name:
        return ""
    original_url = page.url
    try:
        opened = _open_profile_for_row(page, row)
        if not opened:
            row["linkedin_ui_fallback_error"] = "could_not_open_profile_for_linkedin"
            return ""
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        time.sleep(1)
        details = _extract_profile_details(page)
        linkedin_url = details.get("linkedin_profile_url") or _click_profile_linkedin_button(page, name)
        return linkedin_url or ""
    except Exception as exc:
        logger.warning("Could not extract Columbia LinkedIn URL from profile UI for %s: %s", name, exc)
        row["linkedin_ui_fallback_error"] = str(exc)
        return ""
    finally:
        try:
            if page.url != original_url:
                page.goto(original_url, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass


def _auth_probe_succeeded(adapter: ColumbiaAdapter, page) -> bool:
    probe = coveo_search(page, first_result=0, number_of_results=1, max_retries=1)
    if isinstance(probe, dict) and extract_coveo_rows(probe, source_page=1, per_page=1):
        return True
    legacy_probe = api_fetch(page, adapter.build_listing_url(1, 1), max_retries=1)
    return isinstance(legacy_probe, dict) and (
        adapter.extract_total_users(legacy_probe) is not None
        or bool(adapter.extract_listing_users(legacy_probe))
    )


def _refresh_tokens_in_subprocess(headless: bool) -> dict | None:
    script = (
        "from src.utils import setup_logging; "
        "from src.columbia.auth import refresh_tokens; "
        "setup_logging(); "
        f"tokens = refresh_tokens(headless={headless!r}); "
        "raise SystemExit(0 if tokens else 1)"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=os.getcwd(), check=False)
    if result.returncode != 0:
        return None
    return load_cached_tokens()


def _close_browser(browser) -> None:
    try:
        browser.close()
    except Exception:
        pass


def _write_csv(output_path: str, rows: list[dict]) -> None:
    if not rows:
        raise RuntimeError("No Columbia rows to export.")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row.keys()})
    priority = [
        "source",
        "id",
        "external_id",
        "full_name",
        "firstname",
        "lastname",
        "maidenname",
        "class_year",
        "email",
        "current_job",
        "company_name",
        "city",
        "state",
        "country",
        "class_tag",
        "linkedin_profile_url",
        "linkedin_extraction_method",
        "linkedin_visibility",
        "linkedin_url_updated",
        "social_links_private",
        "social_media_account_visibility",
        "profile_url",
        "profile_open_error",
        "salesforce_user_id",
        "permanent_id",
        "current_title",
        "current_company",
        "industry",
        "headline",
    ]
    ordered = [field for field in priority if field in fieldnames]
    ordered.extend(field for field in fieldnames if field not in set(ordered))
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ordered, restval="", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _extract_visible_search_results(page, count: int) -> list[dict]:
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector("text=Results", timeout=15000)
    except Exception:
        logger.warning("Columbia search results heading was not visible before timeout.")

    rows = page.evaluate(
        """(count) => {
            const bodyText = document.body ? document.body.innerText : '';
            const afterSort = bodyText.split('RELEVANCE').pop() || '';
            const resultText = afterSort
                .split(/Results per page|About The Columbia Alumni Association/i)[0];
            const lines = resultText
                .split(/\\n+/)
                .map((line) => line.trim().replace(/\\s+/g, ' '))
                .filter(Boolean);
            const skipPatterns = [
                /^Home$/i, /^Groups$/i, /^Directory$/i, /^CAA Gmail$/i,
                /^ALL$/i, /^ALUMNI$/i, /^GROUPS$/i, /^DISCUSSIONS$/i,
                /^TOPICS$/i, /^Search$/i, /^Refresh$/i, /^Skip to/i,
                /^School$/i, /^Degree Year$/i, /^Country$/i, /^State/i,
                /^City$/i, /^Organization$/i, /^Industry$/i, /^Title$/i,
                /^Results /i, /^\\d+$/, /^\\d+\\s*$/,
            ];
            const seen = new Set();
            const rows = [];

            function profileHrefFor(name) {
                const anchors = Array.from(document.querySelectorAll('a[href]'));
                const lowerName = name.toLowerCase();
                for (const anchor of anchors) {
                    const label = (anchor.innerText || anchor.textContent || '').trim().replace(/\\s+/g, ' ');
                    const lowerLabel = label.toLowerCase();
                    if (!label || lowerLabel === 'learn more') continue;
                    if (lowerLabel === lowerName || lowerName.startsWith(lowerLabel) || lowerLabel.startsWith(lowerName)) {
                        const href = anchor.href || '';
                        if (href && !href.startsWith('javascript:')) return href;
                    }
                }
                return '';
            }

            for (const line of lines) {
                if (skipPatterns.some((pattern) => pattern.test(line))) continue;
                if (line.length > 120) continue;
                const match = line.match(/^(.+?)(\\d{2}[A-Z]{1,5}(?:\\s+\\d{2}[A-Z]{1,5})*)$/);
                if (!match) continue;
                const name = match[1].trim();
                const classTag = match[2].trim();
                if (!name || name.length < 3 || seen.has(name)) continue;
                if (/[0-9|]/.test(name)) continue;
                if (!/[a-z]/.test(name)) continue;
                if (!/[A-Za-z]/.test(name) || /^(Fort Lee|New York|Nanjing|Brooklyn|Jersey City)$/i.test(name)) continue;

                if (seen.has(name)) continue;
                seen.add(name);
                rows.push({
                    source: 'columbia',
                    full_name: name,
                    profile_url: profileHrefFor(name),
                    class_tag: classTag,
                    raw_text: line
                });
                if (rows.length >= count) break;
            }
            return rows;
        }""",
        count,
    )
    return rows if isinstance(rows, list) else []


def _external_id_for_row(row: dict) -> str:
    basis = "|".join(
        str(row.get(key) or "")
        for key in ("full_name", "class_tag", "raw_text", "profile_url")
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


def _set_results_per_page(page, per_page: int) -> None:
    if per_page not in (10, 25, 50, 100):
        logger.info("Columbia per-page %s is not a visible option; leaving page size unchanged.", per_page)
        return
    clicked = page.evaluate(
        """(perPage) => {
            const label = String(perPage);
            const body = document.body ? document.body.innerText : '';
            if (!body.includes('Results per page')) return false;
            const candidates = Array.from(document.querySelectorAll('a, button, [role="button"], span, div'));
            const resultsPerPageNodes = candidates.filter((element) => {
                const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ');
                return text === 'Results per page';
            });
            const anchorTop = resultsPerPageNodes.length
                ? resultsPerPageNodes[0].getBoundingClientRect().top
                : 0;
            for (const element of candidates) {
                const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ');
                const rect = element.getBoundingClientRect();
                if (text !== label || rect.top < anchorTop - 80 || rect.top > anchorTop + 120) continue;
                const clickable = element.closest('a, button, [role="button"], [onclick]') || element;
                clickable.scrollIntoView({block: 'center'});
                clickable.click();
                return true;
            }
            return false;
        }""",
        per_page,
    )
    if clicked:
        time.sleep(2)


def _go_to_results_page(page, target_page: int) -> None:
    page.goto(
        get_start_url(),
        wait_until="domcontentloaded",
        timeout=30000,
    )
    if not _extract_visible_search_results(page, count=1):
        raise ColumbiaAuthExpiredError("Columbia search results were not visible before profile work")
    current_page = 1
    while current_page < target_page:
        if not _go_to_next_results_page(page, current_page):
            raise RuntimeError(f"Could not navigate to Columbia results page {target_page}")
        current_page += 1


def _go_to_next_results_page(page, current_page: int) -> bool:
    next_page = current_page + 1
    clicked = page.evaluate(
        """(nextPage) => {
            const target = String(nextPage);
            const candidates = Array.from(document.querySelectorAll('a, button, [role="button"], span, div'));
            const resultsPerPageNode = candidates.find((element) => {
                const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ');
                return text === 'Results per page';
            });
            const resultsTop = resultsPerPageNode ? resultsPerPageNode.getBoundingClientRect().top : window.innerHeight;
            for (const element of candidates) {
                const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ');
                const rect = element.getBoundingClientRect();
                if (text !== target) continue;
                if (rect.top < 200 || rect.top > resultsTop + 80) continue;
                const clickable = element.closest('a, button, [role="button"], [onclick]') || element;
                clickable.scrollIntoView({block: 'center'});
                clickable.click();
                return true;
            }
            const next = candidates.find((element) => {
                const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                const label = [
                    element.getAttribute('aria-label') || '',
                    element.getAttribute('title') || '',
                ].join(' ').toLowerCase();
                const rect = element.getBoundingClientRect();
                return rect.top > 200 && rect.top < resultsTop + 100
                    && (text === 'next' || label.includes('next'));
            });
            if (!next) return false;
            const clickable = next.closest('a, button, [role="button"], [onclick]') || next;
            clickable.scrollIntoView({block: 'center'});
            clickable.click();
            return true;
        }""",
        next_page,
    )
    if not clicked:
        return False
    time.sleep(2)
    try:
        page.wait_for_selector("text=Results", timeout=15000)
    except Exception:
        pass
    return bool(_extract_visible_search_results(page, count=1))


def _enrich_rows_from_profiles(page, rows: list[dict]) -> None:
    results_url = page.url
    for row in rows:
        name = row.get("full_name")
        if not name:
            continue
        try:
            opened = _open_profile_for_row(page, row)
            if not opened:
                row["profile_open_error"] = "could_not_open_profile"
                continue
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            time.sleep(1)
            details = _extract_profile_details(page)
            if not details.get("linkedin_profile_url"):
                clicked_linkedin = _click_profile_linkedin_button(page, name)
                if clicked_linkedin:
                    details["linkedin_profile_url"] = clicked_linkedin
            row.update({key: value for key, value in details.items() if value})
        except Exception as exc:
            logger.warning("Could not enrich Columbia profile %s: %s", name, exc)
            row["profile_open_error"] = str(exc)
        finally:
            _return_to_results(page, results_url)


def _open_profile_for_row(page, row: dict) -> bool:
    profile_url = row.get("profile_url")
    if profile_url and not profile_url.startswith("javascript:") and "/global-search/" not in profile_url:
        page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        return _wait_for_profile_page(page, row.get("full_name", ""))

    name = row.get("full_name", "")
    class_tag = row.get("class_tag", "")
    clicked = page.evaluate(
        """({name, classTag}) => {
            const lowerName = name.toLowerCase();
            const containers = Array.from(document.querySelectorAll('li, article, tr, .slds-item, .slds-card, div'));
            const visible = (element) => {
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.top < window.innerHeight;
            };
            for (const container of containers) {
                if (!visible(container)) continue;
                const text = (container.innerText || container.textContent || '').trim().replace(/\\s+/g, ' ');
                if (!text || text.length > 260) continue;
                const lowerText = text.toLowerCase();
                if (!lowerText.includes(lowerName)) continue;
                if (classTag && !text.includes(classTag)) continue;
                const target = Array.from(container.querySelectorAll('a, button, [role="link"], [onclick], span'))
                    .find((element) => {
                        const label = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ');
                        return label && label.toLowerCase().startsWith(lowerName);
                    }) || container.querySelector('a, button, [role="link"], [onclick]') || container;
                target.scrollIntoView({block: 'center'});
                target.click();
                return true;
            }
            return false;
        }""",
        {"name": name, "classTag": class_tag},
    )
    if clicked and _wait_for_profile_page(page, name):
        return True

    try:
        page.get_by_text(name, exact=True).first.click(timeout=5000)
    except Exception:
        clicked = page.evaluate(
            """(name) => {
                const lowerName = name.toLowerCase();
                const candidates = Array.from(document.querySelectorAll('a, button, [role="link"], [onclick], span, div'));
                for (const element of candidates) {
                    const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ');
                    if (!text || text.length > 140) continue;
                    const lowerText = text.toLowerCase();
                    if (!(lowerText === lowerName || lowerText.startsWith(lowerName))) continue;
                    const clickable = element.closest('a, button, [role="link"], [onclick]') || element;
                    clickable.scrollIntoView({block: 'center'});
                    clickable.click();
                    return true;
                }
                return false;
            }""",
            name,
        )
        if not clicked:
            return False
    return _wait_for_profile_page(page, name)


def _wait_for_profile_page(page, name: str) -> bool:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            is_profile = page.evaluate(
                """(name) => {
                    const body = document.body ? document.body.innerText : '';
                    return body.includes(name)
                        && (body.includes('Follow') || body.includes('Send Direct Message') || body.includes('Overview'))
                        && !/Results\\s+\\d+\\s*-\\s*\\d+\\s+of/i.test(body);
                }""",
                name,
            )
            if is_profile:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _extract_profile_details(page) -> dict:
    return page.evaluate(
        """() => {
            function cleanLinkedIn(url) {
                if (!url) return '';
                try {
                    const parsed = new URL(url, location.href);
                    const host = parsed.hostname.toLowerCase().replace(/^www\\./, '');
                    const path = parsed.pathname.toLowerCase();
                    if (!host.endsWith('linkedin.com')) return '';
                    if (!path.startsWith('/in/')) return '';
                    return parsed.href;
                } catch(e) {
                    return '';
                }
            }
            const anchors = Array.from(document.querySelectorAll('a[href]'));
            const linkedinUrls = anchors
                .map((anchor) => anchor.href || '')
                .filter((href) => href.toLowerCase().includes('linkedin.com'));
            const linkedin = linkedinUrls.map(cleanLinkedIn).find(Boolean) || '';
            const body = document.body ? document.body.innerText : '';
            const textLinkedinMatches = Array.from(
                body.matchAll(/https?:\\/\\/[^\\s"']*linkedin\\.com\\/[^\\s"']+/ig)
            ).map((match) => match[0]);
            const textLinkedin = textLinkedinMatches.map(cleanLinkedIn).find(Boolean) || '';
            return {
                profile_url: location.href.includes('/global-search/') ? '' : location.href,
                linkedin_profile_url: linkedin || textLinkedin,
            };
        }"""
    )


def _click_profile_linkedin_button(page, name: str) -> str:
    original_url = page.url
    dom_url = page.evaluate(
        """(name) => {
            function visible(element) {
                const rect = element.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.top < 900;
            }
            function profileBand() {
                const needle = name.toLowerCase();
                const nodes = Array.from(document.querySelectorAll('h1, h2, h3, div, span'))
                    .filter((element) => visible(element));
                const nameNode = nodes.find((element) => {
                    const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                    return text === needle;
                });
                const followNode = nodes.find((element) => {
                    const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                    return text === 'follow';
                });
                const top = nameNode ? nameNode.getBoundingClientRect().bottom : 0;
                const bottom = followNode ? followNode.getBoundingClientRect().top : 900;
                return {top, bottom};
            }
            function attrs(element) {
                const values = [];
                for (const item of [element, element.closest('a, button, [role="button"], [onclick]')]) {
                    if (!item) continue;
                    for (const attr of item.attributes || []) values.push(attr.value || '');
                    if (item.dataset) values.push(...Object.values(item.dataset));
                }
                return values.join(' ');
            }
            const band = profileBand();
            const candidates = Array.from(document.querySelectorAll('a, button, [role="button"], [onclick], span, div'))
                .filter((element) => visible(element))
                .filter((element) => {
                    const rect = element.getBoundingClientRect();
                    return rect.top >= band.top && rect.top <= band.bottom;
                })
                .filter((element) => {
                    const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                    const label = [
                        element.getAttribute('aria-label') || '',
                        element.getAttribute('title') || '',
                        element.getAttribute('data-label') || '',
                    ].join(' ').toLowerCase();
                    return text === 'in' || label.includes('linkedin');
                });
            for (const element of candidates) {
                const text = attrs(element);
                const match = text.match(/https?:\\/\\/[^\\s"'<>]*linkedin\\.com\\/in\\/[^\\s"'<>]+/i);
                if (match) return match[0];
            }
            return '';
        }""",
        name,
    )
    if _personal_linkedin_url(dom_url):
        return dom_url

    captured_urls: list[str] = []

    def capture_linkedin(route):
        captured_urls.append(route.request.url)
        route.abort()

    page.context.route("**/*linkedin.com/**", capture_linkedin)
    try:
        clicked = page.evaluate(
            """(name) => {
                function visible(element) {
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.top < 900;
                }
                function profileBand() {
                    const needle = name.toLowerCase();
                    const nodes = Array.from(document.querySelectorAll('h1, h2, h3, div, span, button'))
                        .filter((element) => visible(element));
                    const nameNode = nodes.find((element) => {
                        const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                        return text === needle;
                    });
                    const followNode = nodes.find((element) => {
                        const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                        return text === 'follow';
                    });
                    const top = nameNode ? nameNode.getBoundingClientRect().bottom : 0;
                    const bottom = followNode ? followNode.getBoundingClientRect().top : 900;
                    return {top, bottom};
                }
                const band = profileBand();
                const candidates = Array.from(document.querySelectorAll('a, button, [role="button"], [onclick], span, div'))
                    .filter((element) => visible(element))
                    .filter((element) => {
                        const rect = element.getBoundingClientRect();
                        return rect.top >= band.top && rect.top <= band.bottom;
                    })
                    .filter((element) => {
                        const text = (element.innerText || element.textContent || '').trim().replace(/\\s+/g, ' ').toLowerCase();
                        const label = [
                            element.getAttribute('aria-label') || '',
                            element.getAttribute('title') || '',
                            element.getAttribute('data-label') || '',
                        ].join(' ').toLowerCase();
                        return text === 'in' || label.includes('linkedin');
                    });
                for (const element of candidates) {
                    const clickable = element.closest('a, button, [role="button"], [onclick]') || element;
                    const rect = clickable.getBoundingClientRect();
                    if (rect.top > 900) continue;
                    clickable.scrollIntoView({block: 'center'});
                    clickable.click();
                    return true;
                }
                return false;
            }""",
            name,
        )
        if not clicked:
            return ""
        try:
            page.wait_for_timeout(2500)
        except Exception:
            pass
        for url in captured_urls:
            linkedin_url = _personal_linkedin_url(url)
            if linkedin_url:
                return linkedin_url
        if page.url != original_url:
            linkedin_url = _personal_linkedin_url(page.url)
            if linkedin_url:
                return linkedin_url
            try:
                page.goto(original_url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        return ""
    finally:
        try:
            page.context.unroute("**/*linkedin.com/**", capture_linkedin)
        except Exception:
            pass


def _personal_linkedin_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    host = parsed.hostname.lower().removeprefix("www.") if parsed.hostname else ""
    if not host.endswith("linkedin.com"):
        return ""
    if not parsed.path.lower().startswith("/in/"):
        return ""
    return url


def _return_to_results(page, results_url: str) -> None:
    try:
        if page.url != results_url:
            page.goto(results_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("text=Results", timeout=15000)
    except Exception as exc:
        logger.warning("Could not return to Columbia results page cleanly: %s", exc)
