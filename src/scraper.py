"""
Directory listing scraper + full profile fetcher.

Uses the authenticated Playwright browser context to make API calls,
bypassing Cloudflare and cookie domain issues that block the requests library.

Two-phase approach:
  Phase 1: Paginate /frontoffice/api/users to get all user IDs + basic data
  Phase 2: Fetch /users/{me}/users/{id}?full_profile=true for each user
"""

import json
import logging
import time

from src.utils import save_progress
from config.settings import Settings

logger = logging.getLogger(__name__)


def scrape_directory(
    tokens: dict,
    settings: Settings,
    progress: dict,
) -> list[dict]:
    """
    Scrape the full alumni directory listing using Playwright's browser context.
    """
    all_users = progress.get("listing_users", [])
    start_page = progress.get("listing_last_page", 0) + 1

    if all_users:
        logger.info(f"Resuming from page {start_page} ({len(all_users)} users cached)")

    from playwright.sync_api import sync_playwright
    from src.auth import restore_browser_session

    with sync_playwright() as p:
        browser, page = restore_browser_session(p, tokens, settings)
        if not page:
            logger.error("Could not set up browser for scraping.")
            return all_users

        try:
            total = _get_total_users(page, settings)
            if total is None:
                logger.error("Could not determine total user count.")
                return all_users

            total_pages = (total + settings.per_page - 1) // settings.per_page
            if settings.max_pages:
                total_pages = min(total_pages, settings.max_pages)

            logger.info(f"Total alumni: {total:,} — {total_pages:,} pages at {settings.per_page}/page")

            for pg in range(start_page, total_pages + 1):
                try:
                    users = _fetch_page(page, settings, pg)
                    if not users:
                        logger.warning(f"Page {pg} returned no users. May have reached the end.")
                        break

                    all_users.extend(users)

                    if pg % 10 == 0:
                        progress["listing_users"] = all_users
                        progress["listing_last_page"] = pg
                        save_progress(progress, settings.progress_file)

                    logger.info(
                        f"Page {pg}/{total_pages}: "
                        f"+{len(users)} users ({len(all_users):,} total)"
                    )
                    time.sleep(settings.listing_delay)

                except KeyboardInterrupt:
                    logger.info("Interrupted! Saving progress...")
                    progress["listing_users"] = all_users
                    progress["listing_last_page"] = pg - 1
                    save_progress(progress, settings.progress_file)
                    raise

                except Exception as e:
                    logger.error(f"Error on page {pg}: {e}")
                    progress["listing_users"] = all_users
                    progress["listing_last_page"] = pg - 1
                    save_progress(progress, settings.progress_file)
                    time.sleep(settings.retry_backoff_base)
                    continue

        finally:
            browser.close()

    progress["listing_users"] = all_users
    progress["listing_last_page"] = total_pages
    progress["listing_complete"] = True
    save_progress(progress, settings.progress_file)
    return all_users


def fetch_full_profiles(
    tokens: dict,
    users: list[dict],
    settings: Settings,
    progress: dict,
) -> list[dict]:
    """
    Fetch full profile details for each user via the Playwright browser.
    Merges the full profile data into each user dict.
    """
    my_user_id = tokens["my_user_id"]
    fetched_ids = set(progress.get("fetched_profile_ids", []))
    total = len(users)

    logger.info(
        f"Full profiles: {len(fetched_ids)} already done, "
        f"{total - len(fetched_ids)} remaining"
    )

    from playwright.sync_api import sync_playwright
    from src.auth import restore_browser_session

    with sync_playwright() as p:
        browser, page = restore_browser_session(p, tokens, settings)
        if not page:
            logger.error("Could not set up browser for profile fetching.")
            return users

        try:
            for i, user in enumerate(users):
                user_id = user["id"]
                if user_id in fetched_ids:
                    continue

                try:
                    profile = _fetch_profile(page, settings, my_user_id, user_id)
                    if profile:
                        user["full_profile"] = profile

                    fetched_ids.add(user_id)
                    done = len(fetched_ids)

                    if done % 100 == 0:
                        pct = (done / total) * 100
                        logger.info(f"Profiles: {done:,}/{total:,} ({pct:.1f}%)")
                        progress["fetched_profile_ids"] = list(fetched_ids)
                        save_progress(progress, settings.progress_file)

                    time.sleep(settings.request_delay)

                except KeyboardInterrupt:
                    logger.info("Interrupted! Saving progress...")
                    progress["fetched_profile_ids"] = list(fetched_ids)
                    save_progress(progress, settings.progress_file)
                    raise

                except Exception as e:
                    logger.error(f"Error fetching profile {user_id}: {e}")
                    time.sleep(settings.retry_backoff_base)
                    continue

        finally:
            browser.close()

    progress["fetched_profile_ids"] = list(fetched_ids)
    progress["profiles_complete"] = True
    save_progress(progress, settings.progress_file)
    return users


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _api_fetch(page, url: str, max_retries: int = 3) -> dict | None:
    """Make an API call from inside the Playwright browser."""
    for attempt in range(1, max_retries + 1):
        try:
            result = page.evaluate(
                """async (url) => {
                    try {
                        const resp = await fetch(url, {
                            headers: {
                                'Accept': 'application/json, text/plain, */*',
                                'x-requested-with': 'XMLHttpRequest'
                            }
                        });
                        if (!resp.ok) {
                            return { _error: true, status: resp.status, body: (await resp.text()).substring(0, 200) };
                        }
                        return await resp.json();
                    } catch(e) {
                        return { _error: true, message: e.toString() };
                    }
                }""",
                url,
            )

            if result and not result.get("_error"):
                return result

            status = result.get("status", "unknown")
            logger.warning(f"API error (status {status}) on attempt {attempt}")

            if attempt < max_retries:
                time.sleep(2 ** attempt)

        except Exception as e:
            logger.warning(f"Fetch error on attempt {attempt}: {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    logger.error(f"All {max_retries} retries failed for {url[:120]}")
    return None


def _get_total_users(page, settings: Settings) -> int | None:
    url = (
        f"{settings.base_url}{settings.users_endpoint}"
        f"?page=1&per_page=1"
        f"&query[exclude_current_user]=false"
        f"&query[last_location]=false"
        f"&query[include_users_with_no_locations]=false"
        f"&sort_by=last_seen_at&order=desc"
    )
    data = _api_fetch(page, url)
    return data.get("total_items") if data else None


def _fetch_page(page, settings: Settings, pg: int) -> list[dict]:
    url = (
        f"{settings.base_url}{settings.users_endpoint}"
        f"?page={pg}&per_page={settings.per_page}"
        f"&query[exclude_current_user]=false"
        f"&query[last_location]=false"
        f"&query[include_users_with_no_locations]=false"
        f"&sort_by=last_seen_at&order=desc"
    )
    data = _api_fetch(page, url)
    return data.get("users", []) if data else []
    

def _fetch_profile(page, settings: Settings, my_user_id: str, target_user_id: int) -> dict | None:
    url = (
        f"{settings.base_url}/users/{my_user_id}"
        f"/users/{target_user_id}?full_profile=true"
    )
    data = _api_fetch(page, url)
    if data is None:
        return None
    return data.get("user", data)