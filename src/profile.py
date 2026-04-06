"""
Full profile fetcher.

Makes individual API calls to get detailed profile data
for each user (education, work history, contact info, etc.).
"""

import logging
import time

from src.utils import make_session, retry_request, save_progress
from config.settings import Settings

logger = logging.getLogger(__name__)


def fetch_all_full_profiles(
    tokens: dict,
    users: list[dict],
    settings: Settings,
    progress: dict,
) -> list[dict]:
    """
    Enrich user dicts with full profile data.

    This is the slow step — one API call per user with rate limiting.
    """
    session = make_session(tokens)
    my_user_id = tokens["my_user_id"]

    fetched_ids = set(progress.get("fetched_profile_ids", []))
    total = len(users)

    logger.info(
        f"Fetching full profiles: {len(fetched_ids)} already done, "
        f"{total - len(fetched_ids)} remaining"
    )

    for i, user in enumerate(users):
        user_id = user["id"]

        if user_id in fetched_ids:
            continue

        try:
            profile = _fetch_profile(session, settings, my_user_id, user_id)
            if profile:
                # Merge full profile data into the user dict
                user["full_profile"] = profile

            fetched_ids.add(user_id)

            # Progress logging
            done = len(fetched_ids)
            if done % 100 == 0:
                pct = (done / total) * 100
                logger.info(f"Profiles: {done:,}/{total:,} ({pct:.1f}%)")

                # Save progress
                progress["fetched_profile_ids"] = list(fetched_ids)
                save_progress(progress, settings.progress_file)

            # Rate limiting
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

    progress["fetched_profile_ids"] = list(fetched_ids)
    progress["profiles_complete"] = True
    save_progress(progress, settings.progress_file)

    return users


def _fetch_profile(session, settings: Settings, my_user_id: str, target_user_id: int) -> dict | None:
    """Fetch full profile for a single user."""
    url = (
        f"{settings.base_url}/users/{my_user_id}"
        f"/users/{target_user_id}"
    )
    params = {"full_profile": "true"}

    resp = retry_request(session, url, params=params, settings=settings)
    if resp is None:
        return None

    data = resp.json()
    return data.get("user", data)
