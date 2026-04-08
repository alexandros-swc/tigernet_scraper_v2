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
    """Fetch full profile + /data endpoint for each user.
    
    Uses asyncio + Playwright async API to run multiple browser tabs
    concurrently on a single thread (avoids the threading issues with
    Playwright's sync API).
    """
    import asyncio
    return asyncio.run(_async_fetch_all_profiles(tokens, users, settings, progress))


async def _async_fetch_all_profiles(
    tokens: dict,
    users: list[dict],
    settings: Settings,
    progress: dict,
) -> list[dict]:
    """Async implementation of parallel profile fetching."""
    my_user_id = tokens["my_user_id"]
    fetched_ids = set(progress.get("fetched_profile_ids", []))
    total = len(users)

    # Build list of users that still need fetching
    to_fetch = [(i, u) for i, u in enumerate(users) if u["id"] not in fetched_ids]

    num_tabs = settings.num_tabs

    logger.info(
        f"Full profiles: {len(fetched_ids)} already done, "
        f"{len(to_fetch)} remaining — using {num_tabs} parallel tabs"
    )

    if not to_fetch:
        return users

    from playwright.async_api import async_playwright
    import asyncio

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            )
        )

        # Inject cookies
        cookies = tokens.get("cookies", {})
        cookie_list = []
        for name, value in cookies.items():
            cookie_list.append({
                "name": name,
                "value": value,
                "domain": "tigernet.princeton.edu",
                "path": "/",
            })
        if cookie_list:
            await context.add_cookies(cookie_list)
            logger.info(f"Injected {len(cookie_list)} cookies into browser context.")

        # Open N tabs
        pages = []
        for i in range(num_tabs):
            page = await context.new_page()
            await page.goto(
                "https://tigernet.princeton.edu/people",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            pages.append(page)
            if i == 0:
                await asyncio.sleep(5)  # First tab needs more init time
                logger.info(f"Browser session restored. Current URL: {page.url}")
            else:
                await asyncio.sleep(1)

        logger.info(f"Opened {len(pages)} browser tabs for parallel fetching.")

        # Shared state (safe because asyncio is single-threaded)
        counter = {"done": len(fetched_ids), "errors": 0, "data_ok": 0, "data_fail": 0}
        work_queue = asyncio.Queue()

        for idx, user in to_fetch:
            await work_queue.put((idx, user))

        async def worker(page, worker_id):
            """Async worker — fetches profiles using its assigned browser tab."""
            while not work_queue.empty():
                try:
                    idx, user = work_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                user_id = user["id"]
                try:
                    # Fetch full profile
                    profile = await _async_fetch_profile(page, settings, my_user_id, user_id)

                    # Fetch /data endpoint
                    profile_data = await _async_fetch_profile_data(page, settings, user_id)

                    if profile:
                        users[idx]["full_profile"] = profile
                    if profile_data:
                        users[idx]["profile_data"] = profile_data
                        counter["data_ok"] += 1
                    else:
                        counter["data_fail"] += 1
                    fetched_ids.add(user_id)
                    counter["done"] += 1
                    done = counter["done"]

                    if done % 50 == 0 or done == total:
                        pct = (done / total) * 100
                        logger.info(
                            f"Profiles: {done:,}/{total:,} ({pct:.1f}%) "
                            f"[errors: {counter['errors']}, "
                            f"data_ok: {counter['data_ok']}, data_fail: {counter['data_fail']}]"
                        )
                        progress["fetched_profile_ids"] = list(fetched_ids)
                        save_progress(progress, settings.progress_file)

                except Exception as e:
                    counter["errors"] += 1
                    logger.error(f"Worker {worker_id}: error on profile {user_id}: {e}")

                await asyncio.sleep(settings.request_delay)

        # Launch all workers concurrently
        try:
            await asyncio.gather(*(worker(pages[i], i) for i in range(num_tabs)))
        except KeyboardInterrupt:
            logger.info("Interrupted! Saving progress...")

        logger.info(
            f"Fetch complete: {counter['done']:,} profiles, "
            f"{counter['errors']} errors, "
            f"data_ok: {counter['data_ok']}, data_fail: {counter['data_fail']}"
        )

        # Save final progress
        progress["fetched_profile_ids"] = list(fetched_ids)
        progress["profiles_complete"] = True
        save_progress(progress, settings.progress_file)

        await browser.close()

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


def _fetch_profile_data(page, settings: Settings, target_user_id: int) -> dict | None:
    """
    Fetch the /data endpoint which contains Student Activities,
    Volunteer Activities, Princeton Information, and other custom fields
    not available in the full_profile endpoint.
    
    URL pattern: /users/{target_id}/users/{target_id}/data
    (uses the target user's ID twice)
    """
    url = (
        f"{settings.base_url}/users/{target_user_id}"
        f"/users/{target_user_id}/data"
    )
    data = _api_fetch(page, url)
    if data and "center" in data:
        logger.debug(f"Got /data for user {target_user_id} with {len(data.get('center', []))} sections")
    elif data:
        logger.warning(f"/data for user {target_user_id} returned unexpected keys: {list(data.keys())[:5]}")
    else:
        logger.debug(f"/data for user {target_user_id} returned None")
    return data


# ---------------------------------------------------------------------------
# Async helpers (for parallel profile fetching with asyncio)
# ---------------------------------------------------------------------------

_JS_FETCH = """async (url) => {
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
}"""


async def _async_api_fetch(page, url: str, max_retries: int = 3) -> dict | None:
    """Make an API call from inside the async Playwright browser."""
    import asyncio
    for attempt in range(1, max_retries + 1):
        try:
            result = await page.evaluate(_JS_FETCH, url)

            if result and not result.get("_error"):
                return result

            status = result.get("status", "unknown")
            logger.warning(f"API error (status {status}) on attempt {attempt}")

            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)

        except Exception as e:
            logger.warning(f"Fetch error on attempt {attempt}: {e}")
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)

    logger.error(f"All {max_retries} retries failed for {url[:120]}")
    return None


async def _async_fetch_profile(page, settings: Settings, my_user_id: str, target_user_id: int) -> dict | None:
    url = (
        f"{settings.base_url}/users/{my_user_id}"
        f"/users/{target_user_id}?full_profile=true"
    )
    data = await _async_api_fetch(page, url)
    if data is None:
        return None
    return data.get("user", data)


async def _async_fetch_profile_data(page, settings: Settings, target_user_id: int) -> dict | None:
    url = (
        f"{settings.base_url}/users/{target_user_id}"
        f"/users/{target_user_id}/data"
    )
    data = await _async_api_fetch(page, url)
    if data and "center" in data:
        logger.debug(f"Got /data for user {target_user_id} with {len(data.get('center', []))} sections")
    elif data:
        logger.warning(f"/data for user {target_user_id} returned unexpected keys: {list(data.keys())[:5]}")
    else:
        logger.debug(f"/data for user {target_user_id} returned None")
    return data