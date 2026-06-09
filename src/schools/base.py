"""Base interfaces for school-specific scraper adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SchoolAdapter:
    slug: str
    platform: str
    base_url: str

    def build_listing_url(self, page: int, per_page: int) -> str:
        raise NotImplementedError

    def extract_listing_users(self, payload: dict) -> list[dict]:
        raise NotImplementedError

    def extract_total_users(self, payload: dict) -> int | None:
        raise NotImplementedError

    def external_user_id(self, listing_user: dict) -> str:
        raise NotImplementedError

    def build_full_profile_url(self, my_user_id: str, target_user_id: str) -> str:
        raise NotImplementedError

    def build_profile_data_url(self, target_user_id: str) -> str:
        raise NotImplementedError

    def normalize_profile(
        self,
        listing_payload: dict,
        full_profile_payload: dict | None,
        data_payload: dict | None,
    ) -> dict:
        raise NotImplementedError

