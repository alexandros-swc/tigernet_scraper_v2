"""Graduway API client for St. Paul's."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from src.stpauls.adapter import StPaulsAdapter
from src.stpauls.auth import StPaulsAuthExpiredError, authorization_header

logger = logging.getLogger(__name__)


class StPaulsApiClient:
    def __init__(
        self,
        tokens: dict,
        adapter: StPaulsAdapter | None = None,
        timeout: int = 30,
    ):
        self.tokens = tokens
        self.adapter = adapter or StPaulsAdapter()
        self.timeout = timeout
        self.session = requests.Session()

    def search_directory(self, page: int, per_page: int) -> dict:
        return self._request_json(
            "POST",
            "/Directory/Search",
            json_body=self.adapter.build_listing_body(page=page, per_page=per_page),
            referer=f"{self.adapter.base_url}/directory",
        )

    def fetch_profile(self, external_id: str) -> dict:
        return self._request_json(
            "GET",
            f"/UserProfile/id/{external_id}",
            referer=f"{self.adapter.base_url}/user/{external_id}",
        )

    def check(self) -> dict:
        return self._request_json("GET", "/UserTypes")

    def _request_json(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        referer: str | None = None,
        max_retries: int = 3,
    ) -> dict:
        url = f"{self.adapter.api_base_url}{path}"
        for attempt in range(1, max_retries + 1):
            response = self.session.request(
                method=method,
                url=url,
                headers=self._headers(referer=referer),
                json=json_body,
                timeout=self.timeout,
            )
            if response.status_code in (401, 403, 419):
                raise StPaulsAuthExpiredError(
                    f"St. Paul's API returned auth status {response.status_code}"
                )
            if response.status_code == 429 and attempt < max_retries:
                retry_after = _retry_after_seconds(response)
                logger.warning("Rate limited by St. Paul's API; sleeping %ss", retry_after)
                time.sleep(retry_after)
                continue
            if response.status_code >= 500 and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            payload = response.json()
            if _is_auth_failure_payload(payload):
                raise StPaulsAuthExpiredError("St. Paul's API returned auth failure JSON")
            return payload
        raise RuntimeError(f"St. Paul's API request failed after retries: {path}")

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Authorization": authorization_header(self.tokens),
            "Content-Type": "application/json",
            "Horizontalid": self.adapter.horizontal_id,
            "Horizontalname": self.adapter.horizontal_name,
            "Origin": self.adapter.base_url,
            "Referer": referer or f"{self.adapter.base_url}/",
            "Sharedlanguageid": self.tokens.get(
                "language",
                self.adapter.shared_language_id,
            ),
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/149.0.0.0 Safari/537.36"
            ),
        }


def _retry_after_seconds(response: requests.Response) -> int:
    value = response.headers.get("Retry-After")
    if not value:
        return 10
    try:
        return max(1, min(120, int(value)))
    except ValueError:
        return 10


def _is_auth_failure_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    content = payload.get("content")
    if isinstance(content, dict) and content.get("isSuccess") is False:
        message = str(content.get("message") or content.get("error") or "").lower()
        return any(marker in message for marker in ("auth", "login", "token"))
    return False
