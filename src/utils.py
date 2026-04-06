"""
Utility functions for the TigerNet scraper.

Handles HTTP session setup, retry logic with exponential backoff,
logging configuration, and progress persistence.
"""

import json
import logging
import os
import time
from typing import Optional

import requests

from config.settings import Settings

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """Configure logging to both console and file."""
    os.makedirs("output", exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("output/scraper.log"),
        ],
    )
    # Quiet noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)


def make_session(tokens: dict) -> requests.Session:
    """
    Create a requests.Session pre-configured with TigerNet auth tokens.
    """
    session = requests.Session()

    cookies = tokens.get("cookies", {})
    csrf = tokens.get("csrf_token", "")

    # Build the Cookie header string manually — this avoids domain-matching
    # issues that cause requests to silently drop cookies
    cookie_string = "; ".join(f"{k}={v}" for k, v in cookies.items())

    # Set headers to mimic the browser exactly
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/146.0.0.0 Safari/537.36"
        ),
        "x-requested-with": "XMLHttpRequest",
        "Referer": "https://tigernet.princeton.edu/people",
        "Origin": "https://tigernet.princeton.edu",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "Cookie": cookie_string,
    })

    # Add CSRF token
    if csrf:
        session.headers["x-csrf-token"] = csrf

    logger.info(
        f"Session configured with {len(cookies)} cookies, "
        f"CSRF token: {'yes' if csrf else 'no'}"
    )

    return session


def retry_request(
    session: requests.Session,
    url: str,
    params: dict = None,
    settings: Settings = None,
) -> Optional[requests.Response]:
    """
    Make a GET request with retry logic and exponential backoff.

    Returns the Response object on success, or None after all retries fail.
    """
    if settings is None:
        settings = Settings()

    for attempt in range(1, settings.max_retries + 1):
        try:
            resp = session.get(
                url,
                params=params,
                timeout=settings.request_timeout,
            )

            # Success
            if resp.status_code == 200:
                return resp

            # Auth expired — need to re-authenticate
            if resp.status_code in (401, 403):
                logger.error(
                    f"Auth error ({resp.status_code}). "
                    f"Tokens may have expired. Delete {Settings().progress_file} "
                    f"and output/.token_cache.json, then re-run."
                )
                return None

            # Rate limited
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                logger.warning(
                    f"Rate limited (429). Waiting {retry_after}s before retry..."
                )
                time.sleep(retry_after)
                continue

            # Server error — retry
            if resp.status_code >= 500:
                wait = settings.retry_backoff_base ** attempt
                # Log response body for debugging
                try:
                    body_preview = resp.text[:500]
                except Exception:
                    body_preview = "(could not read body)"
                logger.warning(
                    f"Server error ({resp.status_code}) on attempt {attempt}. "
                    f"Body: {body_preview}. "
                    f"Retrying in {wait:.0f}s..."
                )
                time.sleep(wait)
                continue

            # Other client errors
            try:
                body_preview = resp.text[:500]
            except Exception:
                body_preview = "(could not read body)"
            logger.error(f"Request failed: {resp.status_code} — {url} — Body: {body_preview}")
            return None

        except requests.exceptions.Timeout:
            wait = settings.retry_backoff_base ** attempt
            logger.warning(
                f"Timeout on attempt {attempt}. Retrying in {wait:.0f}s..."
            )
            time.sleep(wait)

        except requests.exceptions.ConnectionError:
            wait = settings.retry_backoff_base ** attempt
            logger.warning(
                f"Connection error on attempt {attempt}. Retrying in {wait:.0f}s..."
            )
            time.sleep(wait)

        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return None

    logger.error(f"All {settings.max_retries} retries failed for {url}")
    return None


def load_progress(path: str = None) -> dict:
    """Load scraping progress from disk."""
    if path is None:
        path = Settings().progress_file

    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load progress file: {e}")
        return {}


def save_progress(progress: dict, path: str = None) -> None:
    """Save scraping progress to disk."""
    if path is None:
        path = Settings().progress_file

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(progress, f)
    except Exception as e:
        logger.warning(f"Could not save progress: {e}")