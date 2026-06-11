"""Authentication helpers for the St. Paul's Graduway site.

This module is deliberately independent from src.auth, which is Princeton-only.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_URL = "https://spsalumninetwork.com"
TOKEN_CACHE_PATH = "output/stpauls/.token_cache.json"
BROWSER_PROFILE_PATH = "output/browser-profile/stpauls"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)


class StPaulsAuthExpiredError(RuntimeError):
    """Raised when St. Paul's API authentication is missing or expired."""


def authenticate(headless: bool = False) -> dict | None:
    cached = load_cached_tokens()
    if cached:
        logger.info("Using cached St. Paul's auth token.")
        return cached
    logger.info("No valid cached St. Paul's token. Starting browser login...")
    return browser_login(headless=headless)


def browser_login(headless: bool = False, timeout_seconds: int = 180) -> dict | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error(
            "Playwright is required for St. Paul's authentication. "
            "Install it with: pip install playwright && playwright install chromium"
        )
        return None

    with sync_playwright() as p:
        context = _launch_persistent_context(p, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
            tokens = _read_tokens_from_page(page)
            if not tokens and headless:
                logger.error(
                    "St. Paul's login is required, but browser is headless. "
                    "Run auth-check without --headless first."
                )
                return None

            deadline = time.monotonic() + timeout_seconds
            while not tokens and time.monotonic() < deadline:
                logger.info(
                    "Waiting for St. Paul's login to complete in the browser..."
                )
                time.sleep(5)
                tokens = _read_tokens_from_page(page)

            if not tokens:
                logger.error("Timed out waiting for St. Paul's auth token.")
                return None

            tokens["browser_profile_path"] = get_browser_profile_path()
            _save_cached_tokens(tokens)
            return tokens
        finally:
            context.close()


def get_browser_profile_path() -> str:
    load_dotenv()
    return os.getenv("STPAULS_BROWSER_PROFILE_DIR", BROWSER_PROFILE_PATH)


def load_cached_tokens(allow_expired: bool = False) -> dict | None:
    tokens = _read_token_cache()
    if not tokens:
        return None
    if allow_expired:
        return tokens
    expires_at = get_access_token_expiry(tokens)
    if not expires_at:
        return None
    if time.time() > expires_at.timestamp() - 300:
        logger.info("Cached St. Paul's auth token expired.")
        return None
    return tokens


def inspect_token_cache() -> dict:
    browser_profile_path = get_browser_profile_path()
    browser_profile_exists = os.path.isdir(browser_profile_path)
    tokens = _read_token_cache()
    if not tokens:
        return {
            "exists": False,
            "path": TOKEN_CACHE_PATH,
            "browser_profile_path": browser_profile_path,
            "browser_profile_exists": browser_profile_exists,
        }

    expires_at = get_access_token_expiry(tokens)
    seconds_remaining = None
    if expires_at:
        seconds_remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())

    return {
        "exists": True,
        "path": TOKEN_CACHE_PATH,
        "browser_profile_path": tokens.get("browser_profile_path", browser_profile_path),
        "browser_profile_exists": browser_profile_exists,
        "token_present": bool(tokens.get("auth_token")),
        "token_expires_at": expires_at.isoformat() if expires_at else None,
        "token_seconds_remaining": seconds_remaining,
        "valid_for_startup": seconds_remaining is not None and seconds_remaining > 300,
        "my_user_id": tokens.get("my_user_id"),
    }


def get_access_token_expiry(tokens: dict | None) -> datetime | None:
    if not tokens:
        return None

    payload = _decode_jwt_payload(_bare_token(tokens.get("auth_token", "")))
    exp = payload.get("exp") if payload else None
    if exp:
        return datetime.fromtimestamp(int(exp), timezone.utc)

    raw_expires = tokens.get("auth_token_expires")
    if raw_expires:
        try:
            parsed = parsedate_to_datetime(raw_expires)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    return None


def authorization_header(tokens: dict) -> str:
    auth_token = tokens.get("auth_token", "")
    if auth_token.lower().startswith("bearer "):
        return auth_token
    return f"bearer {auth_token}"


def _launch_persistent_context(playwright_instance, headless: bool):
    profile_path = get_browser_profile_path()
    os.makedirs(profile_path, exist_ok=True)
    logger.info("Using St. Paul's browser profile: %s", profile_path)
    return playwright_instance.chromium.launch_persistent_context(
        user_data_dir=profile_path,
        headless=headless,
        user_agent=USER_AGENT,
    )


def _read_tokens_from_page(page) -> dict | None:
    raw = page.evaluate(
        """() => ({
            authToken: localStorage.getItem('authToken'),
            authTokenExpires: localStorage.getItem('authTokenExpires'),
            culture: localStorage.getItem('culture'),
            language: localStorage.getItem('language'),
            sseClientId: localStorage.getItem('sseClientId')
        })"""
    )
    if not raw or not raw.get("authToken"):
        return None

    token = raw["authToken"]
    payload = _decode_jwt_payload(_bare_token(token)) or {}
    my_user_id = (
        payload.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/sid")
        or payload.get("sid")
        or payload.get("sub")
    )
    return {
        "auth_token": token,
        "auth_token_expires": raw.get("authTokenExpires"),
        "culture": raw.get("culture") or "en-US",
        "language": raw.get("language") or "4",
        "sse_client_id": raw.get("sseClientId"),
        "my_user_id": str(my_user_id) if my_user_id is not None else None,
    }


def _read_token_cache() -> dict | None:
    if not os.path.exists(TOKEN_CACHE_PATH):
        return None
    try:
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Could not load St. Paul's token cache: %s", exc)
        return None


def _save_cached_tokens(tokens: dict) -> None:
    os.makedirs(os.path.dirname(TOKEN_CACHE_PATH), exist_ok=True)
    safe_tokens = dict(tokens)
    with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(safe_tokens, f)
    logger.info("Saved St. Paul's token cache.")


def _bare_token(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("bearer "):
        return token.split(None, 1)[1]
    return token


def _decode_jwt_payload(token: str) -> dict | None:
    if not token:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:
        logger.debug("Could not decode St. Paul's JWT payload: %s", exc)
        return None
