"""
Authentication module for TigerNet.

Handles the Princeton CAS + Duo MFA login flow using Playwright,
then extracts session tokens for use with the requests library.

Auth flow:
    1. Navigate to tigernet.princeton.edu
    2. Redirected to fed.princeton.edu/cas (Princeton CAS)
    3. Submit NetID + password
    4. Redirected to Duo Security for MFA push
    5. User approves Duo push on phone
    6. Redirected back to TigerNet with session tokens
"""

import json
import logging
import os
import base64
import time

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Cookies we need to keep for API access
REQUIRED_COOKIES = [
    "api_access_token",
    "api_refresh_token",
    "_hivebrite_session",
    "remember_user_token",
    "cf_clearance",
    "__cf_bm",
]

TOKEN_CACHE_PATH = "output/.token_cache.json"


def authenticate(headless: bool = False) -> dict | None:
    """
    Authenticate with TigerNet and return session tokens.

    First tries to load cached tokens. If they're expired or missing,
    launches a browser for interactive login.

    Returns:
        dict with keys: cookies (dict), csrf_token (str), my_user_id (str)
        or None if authentication fails.
    """
    # Try cached tokens first
    cached = _load_cached_tokens()
    if cached:
        logger.info("Using cached authentication tokens.")
        return cached

    # Need fresh login
    logger.info("No valid cached tokens. Starting browser login...")
    return _browser_login(headless)


def _browser_login(headless: bool) -> dict | None:
    """Perform interactive browser login via CAS + Duo."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error(
            "Playwright is required for authentication. "
            "Install it with: pip install playwright && playwright install chromium"
        )
        return None

    load_dotenv()
    netid = os.getenv("PRINCETON_NETID")
    password = os.getenv("PRINCETON_PASSWORD")

    if not netid or not password:
        logger.error(
            "Missing credentials. Set PRINCETON_NETID and PRINCETON_PASSWORD "
            "in your .env file."
        )
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        try:
            # Step 1: Navigate to TigerNet
            logger.info("Navigating to TigerNet...")
            page.goto("https://tigernet.princeton.edu", wait_until="domcontentloaded", timeout=30000)
            # Give the page time to fully render (JS, cookie banner, etc.)
            time.sleep(5)

            # Step 2: Dismiss cookie consent banner — MUST happen before Login click
            logger.info("Looking for cookie consent banner...")
            cookie_dismissed = False
            for cookie_selector in [
                "button:has-text('Accept all cookies')",
                "text=Accept all cookies",
                "button:has-text('Accept')",
                "button.accept-all",
                "[data-testid='cookie-accept']",
                "button:has-text('Reject all')",  # Either option dismisses it
                "button.css-1litn2c",  # Generic button class fallback
            ]:
                try:
                    btn = page.locator(cookie_selector).first
                    if btn.is_visible(timeout=2000):
                        logger.info(f"Dismissing cookie banner via: {cookie_selector}")
                        btn.click()
                        cookie_dismissed = True
                        time.sleep(2)
                        break
                except Exception:
                    continue

            if not cookie_dismissed:
                # Try clicking the X close button on the banner
                try:
                    close_btn = page.locator("button[aria-label='Close'], .close-btn, button:has-text('×')").first
                    if close_btn.is_visible(timeout=2000):
                        logger.info("Closing cookie banner via X button...")
                        close_btn.click()
                        cookie_dismissed = True
                        time.sleep(2)
                except Exception:
                    pass

            if not cookie_dismissed:
                logger.warning("Could not dismiss cookie banner. Trying to proceed anyway...")

            # Step 3: Click the Login button on the TigerNet landing page
            logger.info("Clicking Login button...")
            login_clicked = False
            for selector in [
                "a:has-text('Login')",
                "button:has-text('Login')",
                "a:has-text('Log in')",
                "button:has-text('Log in')",
                "text=Login",
                "text=Log in",
            ]:
                try:
                    btn = page.locator(selector).first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        login_clicked = True
                        logger.info(f"Clicked login button via: {selector}")
                        break
                except Exception:
                    continue

            if not login_clicked:
                # Last resort: navigate directly to CAS login URL
                logger.info("Login button not found. Navigating directly to CAS...")
                page.goto(
                    "https://tigernet.princeton.edu/cas/auth",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )

            # Step 4: Wait for CAS login form to appear and fill it
            logger.info("Waiting for CAS login form...")
            page.wait_for_selector("#username", timeout=30000)
            logger.info("Filling CAS credentials...")
            page.fill("#username", netid)
            page.fill("#password", password)
            page.click('button[type="submit"], input[type="submit"]')

            # Step 5: Wait for Duo MFA
            logger.info(
                "Waiting for Duo MFA approval... "
                "Please approve the push notification on your phone."
            )
            # Duo will either auto-push or show a prompt.
            # We wait up to 2 minutes for the user to approve.
            page.wait_for_url(
                "**/my-homepage**",
                timeout=120000,
            )
            logger.info("Duo approved! Logged into TigerNet.")

            # Step 4: Extract cookies and CSRF token
            cookies_list = context.cookies()
            cookies = {}
            for c in cookies_list:
                if c["name"] in REQUIRED_COOKIES:
                    cookies[c["name"]] = c["value"]

            # Get CSRF token from the page's meta tag or by making an API call
            csrf_token = _extract_csrf_token(page)

            # Extract user ID from the JWT access token
            my_user_id = _extract_user_id_from_jwt(
                cookies.get("api_access_token", "")
            )

            if not my_user_id:
                logger.error("Could not extract user ID from access token.")
                return None

            tokens = {
                "cookies": cookies,
                "csrf_token": csrf_token,
                "my_user_id": my_user_id,
            }

            # Cache tokens for reuse
            _save_cached_tokens(tokens)

            return tokens

        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            # Take a screenshot for debugging
            try:
                page.screenshot(path="output/auth_error.png")
                logger.info("Saved error screenshot to output/auth_error.png")
            except Exception:
                pass
            return None

        finally:
            browser.close()


def _extract_csrf_token(page) -> str:
    """Extract the CSRF token from the page."""
    try:
        # Try to get it from a meta tag
        token = page.evaluate(
            """() => {
                const meta = document.querySelector('meta[name="csrf-token"]');
                return meta ? meta.content : null;
            }"""
        )
        if token:
            return token
    except Exception:
        pass

    # Fallback: make a request to get headers
    try:
        response = page.evaluate(
            """async () => {
                const resp = await fetch('/frontoffice/api/users?page=1&per_page=1', {
                    headers: {'Accept': 'application/json'}
                });
                return resp.ok;
            }"""
        )
    except Exception:
        pass

    logger.warning("Could not extract CSRF token. Some requests may fail.")
    return ""


def _extract_user_id_from_jwt(token: str) -> str | None:
    """Decode the JWT access token to get the user ID."""
    if not token:
        return None

    try:
        # JWT has 3 parts: header.payload.signature
        parts = token.split(".")
        if len(parts) != 3:
            return None

        # Decode the payload (add padding if needed)
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding

        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)

        # User ID is in ext.user_id
        user_id = data.get("ext", {}).get("user_id")
        if user_id:
            logger.info(f"Authenticated as user ID: {user_id}")
        return user_id

    except Exception as e:
        logger.warning(f"Could not decode JWT: {e}")
        return None


def _load_cached_tokens() -> dict | None:
    """Load tokens from cache file if they exist and are still valid."""
    if not os.path.exists(TOKEN_CACHE_PATH):
        return None

    try:
        with open(TOKEN_CACHE_PATH, "r") as f:
            tokens = json.load(f)

        # Check if access token is still valid by decoding its exp claim
        access_token = tokens.get("cookies", {}).get("api_access_token", "")
        if not access_token:
            return None

        parts = access_token.split(".")
        if len(parts) != 3:
            return None

        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding

        decoded = json.loads(base64.urlsafe_b64decode(payload))
        exp = decoded.get("exp", 0)

        import time
        if time.time() > exp - 300:  # 5 minute buffer
            logger.info("Cached tokens expired. Need fresh login.")
            return None

        return tokens

    except Exception as e:
        logger.warning(f"Could not load cached tokens: {e}")
        return None


def _save_cached_tokens(tokens: dict) -> None:
    """Save tokens to cache file."""
    try:
        os.makedirs(os.path.dirname(TOKEN_CACHE_PATH), exist_ok=True)
        with open(TOKEN_CACHE_PATH, "w") as f:
            json.dump(tokens, f)
        logger.info("Saved tokens to cache.")
    except Exception as e:
        logger.warning(f"Could not cache tokens: {e}")