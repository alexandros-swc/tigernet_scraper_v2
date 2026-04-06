"""
Directory listing scraper.

Uses the authenticated Playwright browser context to make API calls,
bypassing Cloudflare and cookie domain issues that block the requests library.
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

    # Use Playwright to make API calls from inside the browser
    from playwright.sync_api import sync_playwright
    from src.auth import restore_browser_session

    with sync_playwright() as p:
        browser, page = restore_browser_session(p, tokens, settings)
        if not page:
            logger.error("Could not set up browser for scraping.")
            return all_users

        try:
            # First request to get total count
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

                    # Save progress periodically
                    if pg % 10 == 0:
                        progress["listing_users"] = all_users
                        progress["listing_last_page"] = pg
                        save_progress(progress, settings.progress_file)

                    logger.info(
                        f"Page {pg}/{total_pages}: "
                        f"+{len(users)} users ({len(all_users):,} total)"
                    )

                    # Rate limiting
                    time.sleep(settings.listing_delay)

                except KeyboardInterrupt:
                    logger.info("Interrupted! Saving progress...")
                    progress["listing_users"] = all_users
                    progress["listing_last_page"] = pg - 1
                    save_progress(progress, settings.progress_file)
                    logger.info(f"Progress saved at page {pg - 1}. Run with --resume to continue.")
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

    # Final save
    progress["listing_users"] = all_users
    progress["listing_last_page"] = total_pages
    progress["listing_complete"] = True
    save_progress(progress, settings.progress_file)

    return all_users


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
            logger.warning(f"API error (status {status}) on attempt {attempt} for {url}")

            if attempt < max_retries:
                time.sleep(2 ** attempt)

        except Exception as e:
            logger.warning(f"Fetch error on attempt {attempt}: {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    logger.error(f"All {max_retries} retries failed for {url}")
    return None


def _get_total_users(page, settings: Settings) -> int | None:
    """Get the total number of users in the directory."""
    url = (
        f"{settings.base_url}{settings.users_endpoint}"
        f"?page=1&per_page=1"
        f"&query[exclude_current_user]=false"
        f"&query[last_location]=false"
        f"&query[include_users_with_no_locations]=false"
        f"&sort_by=last_seen_at&order=desc"
    )

    data = _api_fetch(page, url)
    if data is None:
        return None

    return data.get("total_items")


def _fetch_page(page, settings: Settings, pg: int) -> list[dict]:
    """Fetch a single page of directory results."""
    url = (
        f"{settings.base_url}{settings.users_endpoint}"
        f"?page={pg}&per_page={settings.per_page}"
        f"&query[exclude_current_user]=false"
        f"&query[last_location]=false"
        f"&query[include_users_with_no_locations]=false"
        f"&sort_by=last_seen_at&order=desc"
    )

    data = _api_fetch(page, url)
    if data is None:
        return []

    return data.get("users", [])