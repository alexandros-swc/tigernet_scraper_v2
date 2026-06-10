"""Authentication health checks for long-running scraper operations."""

from __future__ import annotations

from config.settings import Settings
from src.auth import (
    AuthExpiredError,
    authenticate,
    inspect_token_cache,
    load_cached_tokens,
    restore_browser_session,
)
from src.scraper import _api_fetch


def check_auth(
    headless: bool = True,
    login_if_needed: bool = False,
    api_check: bool = True,
) -> dict:
    """Inspect cached auth state and optionally verify it against TigerNet."""
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
            "reason": "no valid cached tokens; pass --login-if-needed to perform login",
        }
        return result

    settings = Settings(headless=headless)
    url = (
        f"{settings.base_url}{settings.users_endpoint}"
        "?page=1&per_page=1"
        "&query[exclude_current_user]=false"
        "&query[last_location]=false"
        "&query[include_users_with_no_locations]=false"
        "&sort_by=last_seen_at&order=desc"
    )

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page = restore_browser_session(p, tokens, settings)
        try:
            payload = _api_fetch(page, url, max_retries=1)
            if payload:
                result["api_check"] = {
                    "status": "ok",
                    "total_items": payload.get("total_items"),
                    "keys": sorted(payload.keys())[:20],
                }
            else:
                result["api_check"] = {
                    "status": "failed",
                    "reason": "API check returned no payload",
                }
        except AuthExpiredError as exc:
            result["api_check"] = {
                "status": "auth_required",
                "reason": str(exc),
            }
        finally:
            browser.close()

    return result
