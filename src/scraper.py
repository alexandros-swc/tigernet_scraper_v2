"""
Directory listing scraper.

Paginates through the /frontoffice/api/users endpoint
to collect basic profile data for all alumni.
"""

import logging
import time

import requests

from src.utils import make_session, retry_request, save_progress
from config.settings import Settings

logger = logging.getLogger(__name__)


def scrape_directory(
    tokens: dict,
    settings: Settings,
    progress: dict,
) -> list[dict]:
    """
    Scrape the full alumni directory listing.

    Returns a list of user dicts with basic profile info.
    """
    session = make_session(tokens)
    all_users = progress.get("listing_users", [])
    start_page = progress.get("listing_last_page", 0) + 1

    if all_users:
        logger.info(f"Resuming from page {start_page} ({len(all_users)} users cached)")

    # First request to get total count
    total = _get_total_users(session, settings)
    if total is None:
        logger.error("Could not determine total user count. Check authentication.")
        return all_users

    total_pages = (total + settings.per_page - 1) // settings.per_page
    if settings.max_pages:
        total_pages = min(total_pages, settings.max_pages)

    logger.info(f"Total alumni: {total:,} — {total_pages:,} pages at {settings.per_page}/page")

    for page in range(start_page, total_pages + 1):
        try:
            users = _fetch_page(session, settings, page)

            if not users:
                logger.warning(f"Page {page} returned no users. May have reached the end.")
                break

            all_users.extend(users)

            # Save progress periodically
            if page % 10 == 0:
                progress["listing_users"] = all_users
                progress["listing_last_page"] = page
                save_progress(progress, settings.progress_file)

            logger.info(
                f"Page {page}/{total_pages}: "
                f"+{len(users)} users ({len(all_users):,} total)"
            )

            # Rate limiting
            time.sleep(settings.listing_delay)

        except KeyboardInterrupt:
            logger.info("Interrupted! Saving progress...")
            progress["listing_users"] = all_users
            progress["listing_last_page"] = page - 1
            save_progress(progress, settings.progress_file)
            logger.info(f"Progress saved at page {page - 1}. Run with --resume to continue.")
            raise

        except Exception as e:
            logger.error(f"Error on page {page}: {e}")
            # Save progress and continue
            progress["listing_users"] = all_users
            progress["listing_last_page"] = page - 1
            save_progress(progress, settings.progress_file)
            time.sleep(settings.retry_backoff_base)
            continue

    # Final save
    progress["listing_users"] = all_users
    progress["listing_last_page"] = total_pages
    progress["listing_complete"] = True
    save_progress(progress, settings.progress_file)

    return all_users


def _get_total_users(session: requests.Session, settings: Settings) -> int | None:
    """Get the total number of users in the directory."""
    url = f"{settings.base_url}{settings.users_endpoint}"
    params = {
        "page": 1,
        "per_page": 1,
        "query[exclude_current_user]": "false",
        "query[last_location]": "false",
        "query[include_users_with_no_locations]": "false",
        "sort_by": "last_name",
        "order": "asc",
    }

    resp = retry_request(session, url, params=params, settings=settings)
    if resp is None:
        return None

    data = resp.json()
    return data.get("total_items")


def _fetch_page(
    session: requests.Session,
    settings: Settings,
    page: int,
) -> list[dict]:
    """Fetch a single page of directory results."""
    url = f"{settings.base_url}{settings.users_endpoint}"
    params = {
        "page": page,
        "per_page": settings.per_page,
        "query[exclude_current_user]": "false",
        "query[last_location]": "false",
        "query[include_users_with_no_locations]": "false",
        "sort_by": "last_name",
        "order": "asc",
    }

    resp = retry_request(session, url, params=params, settings=settings)
    if resp is None:
        return []

    data = resp.json()
    return data.get("users", [])
