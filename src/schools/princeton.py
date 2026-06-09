"""Princeton TigerNet adapter."""

from __future__ import annotations

from src.schools.base import SchoolAdapter


class PrincetonAdapter(SchoolAdapter):
    def __init__(self):
        super().__init__(
            slug="princeton",
            platform="hivebrite",
            base_url="https://tigernet.princeton.edu",
        )

    def build_listing_url(self, page: int, per_page: int) -> str:
        return (
            f"{self.base_url}/frontoffice/api/users"
            f"?page={page}&per_page={per_page}"
            f"&query[exclude_current_user]=false"
            f"&query[last_location]=false"
            f"&query[include_users_with_no_locations]=false"
            f"&sort_by=last_seen_at&order=desc"
        )

    def extract_listing_users(self, payload: dict) -> list[dict]:
        users = payload.get("users", [])
        return users if isinstance(users, list) else []

    def extract_total_users(self, payload: dict) -> int | None:
        total = payload.get("total_items")
        return int(total) if total is not None else None

    def external_user_id(self, listing_user: dict) -> str:
        return str(listing_user["id"])

    def build_full_profile_url(self, my_user_id: str, target_user_id: str) -> str:
        return (
            f"{self.base_url}/users/{my_user_id}"
            f"/users/{target_user_id}?full_profile=true"
        )

    def build_profile_data_url(self, target_user_id: str) -> str:
        return (
            f"{self.base_url}/users/{target_user_id}"
            f"/users/{target_user_id}/data"
        )

    def normalize_profile(
        self,
        listing_payload: dict,
        full_profile_payload: dict | None,
        data_payload: dict | None,
    ) -> dict:
        from src.exporter import _flatten_user

        merged = dict(listing_payload)
        if full_profile_payload:
            merged["full_profile"] = full_profile_payload
        if data_payload:
            merged["profile_data"] = data_payload
        return _flatten_user(merged, full_profiles=True)

