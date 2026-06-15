"""Browser-context API helper for Columbia's Hivebrite directory."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class ColumbiaAuthExpiredError(RuntimeError):
    """Raised when Columbia API responses indicate unusable authentication."""


_AUTH_EXPIRED_STATUSES = {401, 403, 419}
_AUTH_EXPIRED_BODY_MARKERS = (
    "cas",
    "duo",
    "login",
    "sign in",
    "session expired",
    "unauthorized",
)


def api_fetch(page, url: str, max_retries: int = 3) -> dict | None:
    """Make an API call from inside an authenticated Playwright page."""
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
                        const text = await resp.text();
                        if (!resp.ok) {
                            return { _error: true, status: resp.status, body: text.substring(0, 500) };
                        }
                        try {
                            return JSON.parse(text);
                        } catch(e) {
                            return {
                                _error: true,
                                status: resp.status,
                                body: text.substring(0, 500),
                                message: e.toString()
                            };
                        }
                    } catch(e) {
                        return { _error: true, message: e.toString() };
                    }
                }""",
                url,
            )

            if result and not result.get("_error"):
                return result

            auth_reason = _auth_failure_reason(result)
            if auth_reason:
                raise ColumbiaAuthExpiredError(f"{auth_reason} while fetching {url[:120]}")

            status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
            logger.warning("Columbia API error (status %s) on attempt %s", status, attempt)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        except ColumbiaAuthExpiredError:
            raise
        except Exception as exc:
            logger.warning("Columbia fetch error on attempt %s: %s", attempt, exc)
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    logger.error("All %s retries failed for %s", max_retries, url[:120])
    return None


def _auth_failure_reason(result: dict | None) -> str | None:
    if not isinstance(result, dict) or not result.get("_error"):
        return None

    status = result.get("status")
    body = str(result.get("body") or result.get("message") or "").lower()
    if status in _AUTH_EXPIRED_STATUSES:
        return f"API returned auth-like status {status}"
    if body and any(marker in body for marker in _AUTH_EXPIRED_BODY_MARKERS):
        return "API response body looked like a login/session failure"
    return None
