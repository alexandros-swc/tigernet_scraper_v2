"""Seed profile jobs from fast directory listing endpoints."""

from __future__ import annotations

import logging
import math

from config.settings import Settings
from src.auth import authenticate, restore_browser_session
from src.schools import get_adapter
from src.scraper import _api_fetch
from src.storage.db import connection, ensure_schema
from src.storage.raw_store import RawStore
from src.storage.repositories import ScrapeRepository

logger = logging.getLogger(__name__)


def seed_school(
    school_slug: str,
    database_url: str | None = None,
    run_id: int | None = None,
    per_page: int = 100,
    max_pages: int | None = None,
    headless: bool = False,
    raw_root: str = "output/raw",
) -> dict:
    """Populate alumni_seed and profile_jobs for a school."""
    adapter = get_adapter(school_slug)
    raw_store = RawStore(raw_root)
    settings = Settings(per_page=per_page, max_pages=max_pages, headless=headless)

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
                notes=f"Seed run for {school_slug}",
            )

        logger.info("Authenticating for %s seed run %s", school_slug, run["id"])
        tokens = authenticate(headless=headless)
        if not tokens:
            raise RuntimeError("Authentication failed; cannot seed jobs.")

        from playwright.sync_api import sync_playwright

        total_seeded = 0
        total_jobs = 0
        with sync_playwright() as p:
            browser, page = restore_browser_session(p, tokens, settings)
            try:
                first_payload = _api_fetch(page, adapter.build_listing_url(1, 1))
                if not first_payload:
                    raise RuntimeError("Could not fetch first listing page.")

                total_users = adapter.extract_total_users(first_payload)
                total_pages = None
                if total_users is not None:
                    total_pages = math.ceil(total_users / per_page)
                    if max_pages is not None:
                        total_pages = min(total_pages, max_pages)
                elif max_pages is not None:
                    total_pages = max_pages
                else:
                    raise RuntimeError(
                        "Could not determine total pages; pass --max-pages for a bounded seed."
                    )

                logger.info(
                    "Seeding %s pages for %s (run %s)",
                    total_pages,
                    school_slug,
                    run["id"],
                )

                for page_number in range(1, total_pages + 1):
                    payload = _api_fetch(
                        page,
                        adapter.build_listing_url(page_number, per_page),
                    )
                    if not payload:
                        logger.warning("Listing page %s returned no payload", page_number)
                        continue

                    raw_store.put_json(
                        f"{school_slug}/run_{run['id']}/listing/page_{page_number}.json.gz",
                        payload,
                    )

                    users = adapter.extract_listing_users(payload)
                    if not users:
                        logger.warning("Listing page %s returned no users", page_number)
                        continue

                    for user in users:
                        external_user_id = adapter.external_user_id(user)
                        flat_hint = adapter.normalize_profile(user, None, None)
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
                        "Seeded page %s/%s: %s users",
                        page_number,
                        total_pages,
                        len(users),
                    )
            finally:
                browser.close()

    return {
        "run_id": run["id"],
        "school": school_slug,
        "seeded_rows_seen": total_seeded,
        "jobs_seen": total_jobs,
    }

