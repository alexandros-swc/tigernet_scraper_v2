"""Authentication helpers for Columbia's Salesforce community directory.

This module is deliberately independent from src.auth, which is Princeton-only.
It uses the same broad strategy: a durable Playwright profile preserves CAS/Duo
remembered-device state, then cookies are cached for browser-context scraping.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

TOKEN_CACHE_PATH = "output/columbia/.token_cache.json"
BROWSER_PROFILE_PATH = "output/browser-profile/columbia"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)
AUTH_COOKIE_KEYWORDS = ("sid", "session", "oauth", "token", "sfdc", "xsrf")
REQUIRED_COOKIES: tuple[str, ...] = ()
DEFAULT_START_URL = "https://community.alumni.columbia.edu/s/global-search/%40uri#t=All&sort=relevancy"


class ColumbiaAuthRequiredError(RuntimeError):
    """Raised when Columbia community authentication must be refreshed."""


def get_base_url() -> str:
    load_dotenv()
    return (os.getenv("COLUMBIA_BASE_URL") or "https://community.alumni.columbia.edu").rstrip("/")


def get_start_url() -> str:
    load_dotenv()
    return os.getenv("COLUMBIA_START_URL", DEFAULT_START_URL)


def get_login_path() -> str:
    load_dotenv()
    return os.getenv("COLUMBIA_LOGIN_PATH", "/cas/auth")


def authenticate(headless: bool = False, validate_cached: bool = True) -> dict | None:
    cached = load_cached_tokens()
    if cached:
        if not validate_cached or validate_tokens(cached):
            logger.info("Using cached Columbia auth tokens.")
            return cached
        logger.info("Cached Columbia session is no longer valid. Refreshing...")
    logger.info("No valid cached Columbia tokens. Starting browser login...")
    return browser_login(headless=headless)


def browser_login(headless: bool = False, timeout_seconds: int = 180) -> dict | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.error(
            "Playwright is required for Columbia authentication. "
            "Install it with: pip install playwright && playwright install chromium"
        )
        return None

    load_dotenv()
    uni = os.getenv("COLUMBIA_UNI") or os.getenv("COLUMBIA_NETID")
    password = os.getenv("COLUMBIA_PASSWORD")

    with sync_playwright() as p:
        context = _launch_persistent_context(p, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            base_url = get_base_url()
            start_url = get_start_url()
            logger.info("Navigating to Columbia directory: %s", start_url)
            page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)

            already_authenticated = _looks_authenticated(page)
            if not already_authenticated:
                _dismiss_cookie_banner(page)
                if not _is_columbia_cas_login_page(page):
                    _click_login_or_open_auth(page)

            if _wait_for_authenticated(page, timeout_seconds=5):
                logger.info("Columbia CAS/Duo redirected without a fresh prompt.")
            else:
                if not uni or not password:
                    logger.error(
                        "Missing Columbia credentials. Set COLUMBIA_UNI and "
                        "COLUMBIA_PASSWORD in .env for automatic login."
                    )
                    return None
                _drive_login_until_authenticated(
                    page=page,
                    uni=uni,
                    password=password,
                    timeout_seconds=timeout_seconds,
                    headless=headless,
                )

            if not _wait_for_authenticated(page, 10):
                logger.error(
                    "Timed out before Columbia directory authenticated. If Duo prompted "
                    "in a visible browser, approve it and select remembered device."
                )
                return None

            if not _wait_for_authenticated(page, timeout_seconds=30):
                logger.error("Columbia community page did not become usable after login.")
                return None

            cookies_list = context.cookies()
            cookies = _cookies_by_name(cookies_list)
            auth_cookie_names = _auth_cookie_names(cookies_list)
            logger.info(
                "Found %s Columbia cookies, auth-related: %s",
                len(cookies),
                auth_cookie_names,
            )

            csrf_token = unquote(cookies.get("XSRF-TOKEN", ""))

            my_user_id = os.getenv("COLUMBIA_MY_USER_ID") or _extract_user_id_from_page(page)

            tokens = {
                "base_url": base_url,
                "start_url": start_url,
                "cookies": cookies,
                "cookie_jar": cookies_list,
                "csrf_token": csrf_token,
                "my_user_id": str(my_user_id) if my_user_id else None,
                "authenticated_at": datetime.now(timezone.utc).isoformat(),
                "browser_profile_path": get_browser_profile_path(),
            }
            _save_cached_tokens(tokens)
            return tokens
        except Exception as exc:
            logger.error("Columbia authentication failed: %s", exc)
            try:
                os.makedirs("output/columbia", exist_ok=True)
                page.screenshot(path="output/columbia/auth_error.png")
            except Exception:
                pass
            return None
        finally:
            context.close()


def get_browser_profile_path() -> str:
    load_dotenv()
    return os.getenv("COLUMBIA_BROWSER_PROFILE_DIR", BROWSER_PROFILE_PATH)


def restore_browser_session(
    playwright_instance,
    tokens: dict,
    validate: bool = True,
    timeout_seconds: int = 20,
):
    browser = playwright_instance.chromium.launch(headless=True)
    context = browser.new_context(user_agent=USER_AGENT)
    cookie_list = _cookies_for_context(tokens)
    if cookie_list:
        context.add_cookies(cookie_list)
        logger.info("Injected %s Columbia cookies into browser context.", len(cookie_list))

    page = context.new_page()
    page.goto(tokens.get("start_url") or get_start_url(), wait_until="domcontentloaded", timeout=30000)
    time.sleep(2)
    if validate and not _wait_for_authenticated(page, timeout_seconds=timeout_seconds):
        browser.close()
        raise ColumbiaAuthRequiredError("Cached Columbia session did not restore authenticated community access.")
    return browser, page


def refresh_tokens(headless: bool = False) -> dict | None:
    logger.info("Refreshing Columbia tokens...")
    if os.path.exists(TOKEN_CACHE_PATH):
        os.remove(TOKEN_CACHE_PATH)
    return browser_login(headless=headless)


def validate_tokens(tokens: dict, timeout_seconds: int = 20) -> bool:
    """Verify cached Columbia cookies against the real community page."""
    if not tokens:
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("Cannot validate Columbia tokens because Playwright is unavailable.")
        return False

    try:
        with sync_playwright() as p:
            browser, page = restore_browser_session(
                p,
                tokens,
                validate=False,
                timeout_seconds=timeout_seconds,
            )
            try:
                return _wait_for_authenticated(page, timeout_seconds=timeout_seconds)
            finally:
                browser.close()
    except Exception as exc:
        logger.info("Columbia token validation failed: %s", exc)
        return False


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

    cookies = tokens.get("cookies", {})
    expires_at = get_access_token_expiry(tokens)
    seconds_remaining = None
    if expires_at:
        seconds_remaining = int((expires_at - datetime.now(timezone.utc)).total_seconds())

    return {
        "exists": True,
        "path": TOKEN_CACHE_PATH,
        "base_url": tokens.get("base_url", get_base_url()),
        "start_url": tokens.get("start_url", get_start_url()),
        "browser_profile_path": tokens.get("browser_profile_path", browser_profile_path),
        "browser_profile_exists": browser_profile_exists,
        "my_user_id": tokens.get("my_user_id"),
        "cookie_count": len(cookies),
        "cookie_jar_count": len(tokens.get("cookie_jar") or []),
        "authenticated_at": tokens.get("authenticated_at"),
        "access_token_present": bool(cookies.get("api_access_token")),
        "access_token_expires_at": expires_at.isoformat() if expires_at else None,
        "access_token_seconds_remaining": seconds_remaining,
        "valid_for_startup": seconds_remaining is not None and seconds_remaining > 300,
        "required_cookies_missing": [name for name in REQUIRED_COOKIES if not cookies.get(name)],
    }


def load_cached_tokens(allow_expired: bool = False) -> dict | None:
    tokens = _read_token_cache()
    if not tokens:
        return None
    if allow_expired:
        return tokens
    expires_at = get_access_token_expiry(tokens)
    if not expires_at:
        if tokens.get("cookie_jar") or tokens.get("cookies"):
            return tokens
        return None
    if time.time() > expires_at.timestamp() - 300:
        logger.info("Cached Columbia tokens expired.")
        return None
    return tokens


def get_access_token_expiry(tokens: dict | None) -> datetime | None:
    if not tokens:
        return None
    access_token = tokens.get("cookies", {}).get("api_access_token", "")
    payload = _decode_jwt_payload(access_token)
    exp = payload.get("exp") if payload else None
    if not exp:
        return None
    return datetime.fromtimestamp(int(exp), timezone.utc)


def _launch_persistent_context(playwright_instance, headless: bool):
    profile_path = get_browser_profile_path()
    os.makedirs(profile_path, exist_ok=True)
    logger.info("Using Columbia browser profile: %s", profile_path)
    return playwright_instance.chromium.launch_persistent_context(
        user_data_dir=profile_path,
        headless=headless,
        user_agent=USER_AGENT,
    )


def _looks_authenticated(page) -> bool:
    try:
        current = page.url.lower()
        base_host = urlparse(get_base_url()).hostname or ""
        current_host = urlparse(page.url).hostname or ""
        if base_host and current_host != base_host:
            return False
        if any(marker in current for marker in ("/login", "/secur/", "/idp/", "/saml")):
            return False
        return _has_community_content(page)
    except Exception:
        return False


def _wait_for_api_session(page, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _looks_authenticated(page):
            return True
        time.sleep(1)
    return False


def _wait_for_authenticated(page, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _looks_authenticated(page):
            return True
        time.sleep(1)
    return False


def _wait_for_selector_or_authenticated(page, selector: str, timeout_seconds: int) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _looks_authenticated(page):
            return "authenticated"
        try:
            if page.locator(selector).first.is_visible(timeout=1000):
                return "selector"
        except Exception:
            pass
        time.sleep(0.5)
    return "timeout"


def _dismiss_cookie_banner(page) -> None:
    _click_first_visible(
        page,
        [
            "button:has-text('Accept all cookies')",
            "button:has-text('Accept')",
            "button:has-text('Reject all')",
            "[data-testid='cookie-accept']",
        ],
        timeout_ms=1500,
    )


def _click_login_or_open_auth(page) -> None:
    if _is_columbia_cas_login_page(page):
        logger.info("Already on Columbia CAS login page; not clicking LOGIN before filling credentials.")
        return
    logger.info("Opening Columbia CAS login URL directly; no pre-credential login click.")
    page.goto(f"{get_base_url()}{get_login_path()}", wait_until="domcontentloaded", timeout=30000)


def _drive_login_until_authenticated(
    page,
    uni: str,
    password: str,
    timeout_seconds: int,
    headless: bool,
) -> bool:
    """Automate Columbia credential screens and remembered-device prompts."""
    deadline = time.monotonic() + timeout_seconds
    filled_user = False
    filled_password = False
    last_url = ""

    while time.monotonic() < deadline:
        if _looks_authenticated(page):
            return True

        current_url = page.url
        if current_url != last_url:
            logger.info("Columbia login at: %s", current_url)
            last_url = current_url

        _dismiss_cookie_banner(page)

        if _is_columbia_cas_login_page(page):
            if filled_user and filled_password:
                _click_columbia_cas_login_with_mouse(page)
            elif _fill_columbia_cas_form(page, uni=uni, password=password):
                if not filled_user:
                    logger.info("Filled Columbia CAS UNI/password form.")
                    filled_user = True
                    filled_password = True
                _click_columbia_cas_login(page)
            elif _fill_columbia_cas_with_mouse(page, uni=uni, password=password):
                if not filled_user:
                    logger.info("Filled Columbia CAS UNI/password form.")
                    filled_user = True
                    filled_password = True
                _click_columbia_cas_login_with_mouse(page)
            else:
                logger.warning("Columbia CAS login page is visible, but fields were not filled. Not clicking LOGIN.")
            time.sleep(1)
            continue

        if _fill_columbia_cas_form(page, uni=uni, password=password):
            if not filled_user:
                logger.info("Filled Columbia CAS UNI/password form.")
                filled_user = True
                filled_password = True
            _click_columbia_cas_login(page)

        if _fill_login_input(
            page,
            field_type="username",
            value=uni,
        ):
            if not filled_user:
                logger.info("Filled Columbia UNI/username.")
                filled_user = True
            _click_first_visible(
                page,
                [
                    "button:has-text('Next')",
                    "input[type='submit']",
                    "button[type='submit']",
                    "button:has-text('Continue')",
                    "button:has-text('Sign in')",
                    "button:has-text('Login')",
                    "button:has-text('Log in')",
                ],
                timeout_ms=1000,
            )

        if _fill_login_input(
            page,
            field_type="password",
            value=password,
        ):
            if not filled_password:
                logger.info("Filled Columbia password.")
                filled_password = True
            _click_first_visible(
                page,
                [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button:has-text('Sign in')",
                    "button:has-text('Login')",
                    "button:has-text('Log in')",
                    "button:has-text('Continue')",
                    "button:has-text('Verify')",
                ],
                timeout_ms=1000,
            )

        _click_first_visible(
            page,
            [
                "button:has-text('Send me a Push')",
                "button:has-text('Send Me a Push')",
                "button:has-text('Duo Push')",
                "button:has-text('Trust this browser')",
                "button:has-text('Remember me')",
                "button:has-text('Yes, trust browser')",
                "label:has-text('Remember me')",
                "label:has-text('Trust this browser')",
                "input[type='checkbox']",
                "button:has-text('Continue')",
            ],
            timeout_ms=1000,
        )

        if headless and filled_password and _looks_like_mfa_wait(page):
            logger.info(
                "Columbia login is waiting on MFA. Headless refresh can only "
                "continue automatically if Duo remembered this browser."
            )

        time.sleep(1)

    return _looks_authenticated(page)


def _fill_columbia_cas_form(page, uni: str, password: str) -> bool:
    for frame in page.frames:
        try:
            fields = _visible_input_fields(frame)
            if len(fields) < 2:
                continue
            password_field = next(
                (field for field in fields if field["type"] == "password"),
                None,
            )
            uni_field = next(
                (
                    field
                    for field in fields
                    if field is not password_field
                    and field["type"] not in ("password", "submit", "button")
                ),
                None,
            )
            if not uni_field or not password_field:
                continue

            uni_locator = frame.locator("input").nth(uni_field["index"])
            password_locator = frame.locator("input").nth(password_field["index"])
            _type_into_locator(page, uni_locator, uni)
            _type_into_locator(page, password_locator, password)

            uni_value = uni_locator.input_value(timeout=1000)
            password_value = password_locator.input_value(timeout=1000)
            if uni_value != uni or password_value != password:
                _dump_login_debug(page, reason="values_did_not_stick")
                logger.warning(
                    "Columbia CAS input values did not stick "
                    "(uni_len=%s password_len=%s); not clicking LOGIN.",
                    len(uni_value or ""),
                    len(password_value or ""),
                )
                return False

            frame.evaluate(
                """({uniIndex, passwordIndex}) => {
                    const inputs = Array.from(document.querySelectorAll('input'));
                    if (inputs[uniIndex]) inputs[uniIndex].setAttribute('data-codex-login-field', 'uni');
                    if (inputs[passwordIndex]) inputs[passwordIndex].setAttribute('data-codex-login-field', 'password');
                }""",
                {"uniIndex": uni_field["index"], "passwordIndex": password_field["index"]},
            )
            logger.info(
                "Filled Columbia CAS fields uni index=%s type=%r password index=%s type=%r",
                uni_field["index"],
                uni_field["type"],
                password_field["index"],
                password_field["type"],
            )
            return True
        except Exception as exc:
            logger.debug("Columbia CAS positional fill failed in a frame: %s", exc)
            continue
    _dump_login_debug(page, reason="no_fillable_fields")
    return False


def _fill_columbia_cas_with_mouse(page, uni: str, password: str) -> bool:
    """Fallback for Columbia CAS: click/type by screen position like a human."""
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        width = viewport["width"]
        height = viewport["height"]

        # Defaults match the centered Columbia CAS card. Env overrides allow
        # quick tuning without code changes if a browser size differs.
        uni_x = float(os.getenv("COLUMBIA_LOGIN_UNI_X_RATIO", "0.55")) * width
        uni_y = float(os.getenv("COLUMBIA_LOGIN_UNI_Y_RATIO", "0.72")) * height
        password_x = float(os.getenv("COLUMBIA_LOGIN_PASSWORD_X_RATIO", "0.55")) * width
        password_y = float(os.getenv("COLUMBIA_LOGIN_PASSWORD_Y_RATIO", "0.795")) * height

        page.mouse.click(uni_x, uni_y)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.type(uni, delay=35)

        page.mouse.click(password_x, password_y)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.keyboard.type(password, delay=35)

        if not _visible_credential_values_match(page, uni=uni, password=password):
            logger.warning("Columbia CAS coordinate typing did not populate visible inputs.")
            return False

        logger.info(
            "Typed Columbia CAS credentials by mouse coordinates "
            "(uni %.0f,%.0f password %.0f,%.0f).",
            uni_x,
            uni_y,
            password_x,
            password_y,
        )
        return True
    except Exception as exc:
        logger.warning("Columbia CAS coordinate typing failed: %s", exc)
        return False


def _visible_credential_values_match(page, uni: str, password: str) -> bool:
    for frame in page.frames:
        try:
            matches = frame.evaluate(
                """({uni, password}) => {
                    const values = Array.from(document.querySelectorAll('input')).map((input) => ({
                        type: (input.type || '').toLowerCase(),
                        value: input.value || '',
                        rect: input.getBoundingClientRect(),
                    })).filter((item) => item.rect.width > 0 && item.rect.height > 0);
                    const uniValue = values.find((item) => item.value === uni);
                    const passwordValue = values.find((item) => item.type === 'password' && item.value === password);
                    return !!uniValue && !!passwordValue;
                }""",
                {"uni": uni, "password": password},
            )
            if matches:
                return True
        except Exception:
            continue
    return False


def _visible_input_fields(frame) -> list[dict]:
    return frame.evaluate(
        """() => Array.from(document.querySelectorAll('input'))
            .map((input, index) => {
                const rect = input.getBoundingClientRect();
                const style = window.getComputedStyle(input);
                return {
                    index,
                    type: (input.type || '').toLowerCase(),
                    id: input.id || '',
                    name: input.name || '',
                    placeholder: input.placeholder || '',
                    visible: rect.width > 0
                        && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && !input.disabled
                        && !input.readOnly,
                    top: rect.top,
                    left: rect.left,
                };
            })
            .filter((item) => item.visible)
            .filter((item) => !['hidden', 'submit', 'button', 'checkbox', 'radio', 'search'].includes(item.type))
            .sort((a, b) => (a.top - b.top) || (a.left - b.left));
        """
    )


def _type_into_locator(page, locator, value: str) -> None:
    locator.scroll_into_view_if_needed(timeout=3000)
    box = locator.bounding_box(timeout=3000)
    if not box:
        locator.click(timeout=3000)
    else:
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(value, delay=35)


def _dump_login_debug(page, reason: str) -> None:
    try:
        os.makedirs("output/columbia", exist_ok=True)
        data = {
            "reason": reason,
            "url": page.url,
            "frames": [],
        }
        for frame in page.frames:
            try:
                data["frames"].append(
                    {
                        "url": frame.url,
                        "inputs": _visible_input_fields(frame),
                        "body_preview": frame.evaluate(
                            "() => document.body ? document.body.innerText.slice(0, 1000) : ''"
                        ),
                    }
                )
            except Exception as exc:
                data["frames"].append({"url": frame.url, "error": str(exc)})
        with open("output/columbia/login_debug.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        page.screenshot(path="output/columbia/login_debug.png", full_page=True)
        logger.warning(
            "Saved Columbia login debug files to output/columbia/login_debug.json and login_debug.png"
        )
    except Exception as exc:
        logger.warning("Could not write Columbia login debug dump: %s", exc)


def _is_columbia_cas_login_page(page) -> bool:
    for frame in page.frames:
        try:
            is_login = frame.evaluate(
                """() => {
                    const body = document.body ? document.body.innerText : '';
                    const url = location.href || '';
                    const visibleInputs = Array.from(document.querySelectorAll('input')).filter((input) => {
                        const rect = input.getBoundingClientRect();
                        const style = window.getComputedStyle(input);
                        const type = (input.type || '').toLowerCase();
                        return rect.width > 0
                            && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && !['hidden', 'submit', 'button', 'checkbox', 'radio'].includes(type);
                    });
                    return (
                        /Hackers want your passcodes/i.test(body)
                        || /UNI Help/i.test(body)
                        || (/\\bUNI\\b/i.test(body) && /PASSWORD/i.test(body))
                        || /cas|login|saml|idp/i.test(url)
                    ) && /LOGIN/i.test(body) && visibleInputs.length >= 2;
                }"""
            )
            if is_login:
                return True
        except Exception:
            continue
    return False


def _click_columbia_cas_login(page) -> bool:
    for frame in page.frames:
        try:
            clicked = frame.evaluate(
                """() => {
                    const uniInput = document.querySelector("[data-codex-login-field='uni']");
                    const passwordInput = document.querySelector("[data-codex-login-field='password']");
                    if (!uniInput || !passwordInput || !uniInput.value || !passwordInput.value) {
                        return false;
                    }
                    const candidates = Array.from(document.querySelectorAll('button, input[type="submit"], [role="button"], a'));
                    for (const element of candidates) {
                        const label = (
                            element.innerText
                            || element.value
                            || element.getAttribute('aria-label')
                            || element.textContent
                            || ''
                        ).trim().toLowerCase();
                        if (label !== 'login' && label !== 'log in') continue;
                        element.click();
                        return true;
                    }
                    return false;
                }"""
            )
            if clicked:
                time.sleep(1)
                return True
        except Exception:
            continue
    logger.warning("Columbia CAS fields were filled, but LOGIN button was not clicked.")
    return False


def _click_columbia_cas_login_with_mouse(page) -> bool:
    try:
        viewport = page.viewport_size or {"width": 1280, "height": 720}
        login_x = float(os.getenv("COLUMBIA_LOGIN_BUTTON_X_RATIO", "0.565")) * viewport["width"]
        login_y = float(os.getenv("COLUMBIA_LOGIN_BUTTON_Y_RATIO", "0.875")) * viewport["height"]
        page.mouse.click(login_x, login_y)
        time.sleep(1)
        logger.info("Clicked Columbia CAS LOGIN by mouse coordinates (%.0f,%.0f).", login_x, login_y)
        return True
    except Exception as exc:
        logger.warning("Columbia CAS coordinate LOGIN click failed: %s", exc)
        return False


def _looks_like_mfa_wait(page) -> bool:
    try:
        body = page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''")
        return any(
            marker in body
            for marker in (
                "duo",
                "push",
                "approve",
                "two-factor",
                "multi-factor",
                "verification code",
                "check your device",
            )
        )
    except Exception:
        return False


def _fill_login_input(page, field_type: str, value: str) -> bool:
    for frame in page.frames:
        try:
            result = frame.evaluate(
                """({fieldType, value}) => {
                    function visible(element) {
                        const rect = element.getBoundingClientRect();
                        const style = window.getComputedStyle(element);
                        return rect.width > 0
                            && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && !element.disabled
                            && !element.readOnly;
                    }
                    function labelText(element) {
                        const values = [
                            element.id || '',
                            element.name || '',
                            element.type || '',
                            element.autocomplete || '',
                            element.placeholder || '',
                            element.getAttribute('aria-label') || '',
                            element.getAttribute('title') || '',
                        ];
                        if (element.id) {
                            const explicit = document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
                            if (explicit) values.push(explicit.innerText || explicit.textContent || '');
                        }
                        const parentLabel = element.closest('label');
                        if (parentLabel) values.push(parentLabel.innerText || parentLabel.textContent || '');
                        const nearby = element.closest('div, p, section, form');
                        if (nearby) values.push((nearby.innerText || nearby.textContent || '').slice(0, 240));
                        return values.join(' ').toLowerCase();
                    }
                    function scoreInput(element) {
                        const text = labelText(element);
                        const type = (element.type || '').toLowerCase();
                        if (/search|find|query|filter/.test(text) || type === 'search') return -100;
                        if (fieldType === 'password') {
                            if (type !== 'password' && !/password|passcode/.test(text)) return -100;
                            let score = type === 'password' ? 80 : 20;
                            if (/password/.test(text)) score += 30;
                            if (/current-password/.test(text)) score += 20;
                            return score;
                        }
                        if (type === 'password') return -100;
                        let score = 0;
                        if (/username|userid|user id|login|loginfmt/.test(text)) score += 60;
                        if (/uni|columbia id|network id|netid/.test(text)) score += 50;
                        if (/email|e-mail/.test(text)) score += 25;
                        if (/autocomplete username/.test(text)) score += 25;
                        if (type === 'email') score += 20;
                        if (type === 'text') score += 10;
                        return score || -100;
                    }
                    const inputs = Array.from(document.querySelectorAll('input'))
                        .filter(visible)
                        .map((input) => ({input, score: scoreInput(input)}))
                        .filter((item) => item.score > 0)
                        .sort((a, b) => b.score - a.score);
                    if (!inputs.length) return null;
                    const chosen = inputs[0].input;
                    chosen.focus();
                    chosen.value = '';
                    chosen.dispatchEvent(new Event('input', {bubbles: true}));
                    chosen.value = value;
                    chosen.dispatchEvent(new Event('input', {bubbles: true}));
                    chosen.dispatchEvent(new Event('change', {bubbles: true}));
                    return {
                        id: chosen.id || '',
                        name: chosen.name || '',
                        type: chosen.type || '',
                        placeholder: chosen.placeholder || '',
                        score: inputs[0].score,
                    };
                }""",
                {"fieldType": field_type, "value": value},
            )
            if result:
                logger.info(
                    "Filled Columbia %s field id=%r name=%r type=%r placeholder=%r score=%s",
                    field_type,
                    result.get("id"),
                    result.get("name"),
                    result.get("type"),
                    result.get("placeholder"),
                    result.get("score"),
                )
                return True
        except Exception:
            continue
    return False


def _fill_first_visible(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            item = page.locator(selector).first
            if item.is_visible(timeout=1000):
                item.fill(value)
                return True
        except Exception:
            continue
    return False


def _click_first_visible(page, selectors: list[str], timeout_ms: int = 1000) -> bool:
    for selector in selectors:
        try:
            item = page.locator(selector).first
            if item.is_visible(timeout=timeout_ms):
                item.click()
                time.sleep(1)
                return True
        except Exception:
            continue
    return False


def _extract_user_id_from_api(page) -> str | None:
    try:
        raw = page.evaluate(
            """async () => {
                for (const url of ['/frontoffice/api/header_data', '/frontoffice/api/session_info.json?type=user']) {
                    try {
                        const resp = await fetch(url, {headers: {'Accept': 'application/json'}});
                        if (resp.ok) return await resp.json();
                    } catch(e) {}
                }
                return null;
            }"""
        )
        if not isinstance(raw, dict):
            return None
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        current_user = raw.get("current_user") if isinstance(raw.get("current_user"), dict) else {}
        return raw.get("user_id") or raw.get("id") or user.get("id") or current_user.get("id")
    except Exception as exc:
        logger.warning("Columbia user ID API fallback failed: %s", exc)
        return None


def _has_community_content(page) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                    const body = document.body ? document.body.innerText : '';
                    return body.includes('Search your community')
                        || body.includes('Results 1-')
                        || body.includes('Directory')
                        || body.includes('CAA Gmail');
                }"""
            )
        )
    except Exception:
        return False


def _extract_user_id_from_page(page) -> str | None:
    try:
        return page.evaluate(
            r"""() => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                for (const link of links) {
                    const href = link.href || '';
                    const match = href.match(/\/profile\/([^/?#]+)/) || href.match(/\/user\/([^/?#]+)/);
                    if (match) return match[1];
                }
                return null;
            }"""
        )
    except Exception:
        return None


def _extract_user_id_from_jwt(token: str) -> str | None:
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    user_id = payload.get("ext", {}).get("user_id") if isinstance(payload.get("ext"), dict) else None
    return str(user_id) if user_id else None


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
        logger.debug("Could not decode Columbia JWT payload: %s", exc)
        return None


def _read_token_cache() -> dict | None:
    if not os.path.exists(TOKEN_CACHE_PATH):
        return None
    try:
        with open(TOKEN_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Could not load Columbia token cache: %s", exc)
        return None


def _save_cached_tokens(tokens: dict) -> None:
    os.makedirs(os.path.dirname(TOKEN_CACHE_PATH), exist_ok=True)
    with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(tokens, f)
    logger.info("Saved Columbia token cache.")


def _cookies_by_name(cookies_list: list[dict]) -> dict:
    return {cookie["name"]: cookie["value"] for cookie in cookies_list}


def _auth_cookie_names(cookies_list: list[dict]) -> list[str]:
    names = []
    for cookie in cookies_list:
        name = cookie.get("name", "")
        if any(keyword in name.lower() for keyword in AUTH_COOKIE_KEYWORDS):
            domain = cookie.get("domain", "")
            names.append(f"{domain}:{name}" if domain else name)
    return names


def _cookies_for_context(tokens: dict) -> list[dict]:
    cookie_jar = tokens.get("cookie_jar") or []
    if cookie_jar:
        return [cookie for cookie in (_cookie_for_context(raw) for raw in cookie_jar) if cookie]

    domain = urlparse(tokens.get("base_url") or get_base_url()).hostname or "alumni.columbia.edu"
    cookies = []
    for name, value in tokens.get("cookies", {}).items():
        cookie = _cookie_for_context(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
            }
        )
        if cookie:
            cookies.append(cookie)
    return cookies


def _cookie_for_context(raw_cookie: dict) -> dict | None:
    if not raw_cookie.get("name") or raw_cookie.get("value") is None:
        return None
    allowed_fields = (
        "name",
        "value",
        "url",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    )
    cookie = {
        key: raw_cookie[key]
        for key in allowed_fields
        if key in raw_cookie and raw_cookie[key] is not None
    }
    if "url" not in cookie and "domain" not in cookie:
        cookie["domain"] = urlparse(get_base_url()).hostname or "alumni.columbia.edu"
    if "url" not in cookie and "path" not in cookie:
        cookie["path"] = "/"
    return cookie
