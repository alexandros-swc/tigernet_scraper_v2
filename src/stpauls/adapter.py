"""St. Paul's School Alumni Network response adapter and normalizer."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class StPaulsAdapter:
    slug: str = "stpauls"
    platform: str = "graduway"
    base_url: str = "https://spsalumninetwork.com"
    api_base_url: str = "https://api.ng.prod.us-east1.manual.graduway.com"
    default_per_page: int = 20
    horizontal_id: str = "31242"
    horizontal_name: str = "spsalumninetwork"
    shared_language_id: str = "4"

    def build_listing_body(self, page: int, per_page: int) -> dict:
        return {
            "sortType": 1,
            "freeText": None,
            "displayLostAlumni": False,
            "companyIds": [],
            "paginationFilter": {
                "perPage": per_page,
                "pageNumber": page,
            },
            "totalCount": 0,
            "coordinates": None,
            "bounds": None,
        }

    def extract_listing_users(self, payload: dict) -> list[dict]:
        users = _data(payload).get("directoryUsers", [])
        return users if isinstance(users, list) else []

    def extract_total_users(self, payload: dict) -> int | None:
        total = _data(payload).get("totalCount")
        return int(total) if total is not None else None

    def external_user_id(self, listing_user: dict) -> str:
        external_id = listing_user.get("externalId") or listing_user.get("id")
        if external_id is None:
            raise KeyError("St. Paul's listing user is missing externalId/id")
        return str(external_id)

    def normalize_profile(
        self,
        listing_payload: dict,
        full_profile_payload: dict | None,
    ) -> dict:
        profile = _data(full_profile_payload) if full_profile_payload else {}
        source = profile or listing_payload
        row: dict[str, str] = {
            "source": "stpauls",
            "id": _clean(source.get("id") or listing_payload.get("id")),
            "external_id": _clean(
                source.get("externalId") or listing_payload.get("externalId")
            ),
            "firstname": _clean(source.get("firstName") or listing_payload.get("firstName")),
            "lastname": _clean(source.get("lastName") or listing_payload.get("lastName")),
            "maidenname": _clean(
                source.get("maidenName") or listing_payload.get("maidenName")
            ),
            "pronouns": _clean(source.get("pronouns") or listing_payload.get("pronouns")),
            "email": _clean(profile.get("email")),
            "phone": _clean(profile.get("phone")),
            "birthday": _clean(profile.get("dateOfBirth")),
            "website": _clean(profile.get("website")),
            "summary": _clean(profile.get("summary")),
            "photo_url": _clean(source.get("photoUrl") or listing_payload.get("photoUrl")),
            "is_willing_to_help": _clean(
                source.get("isWillingToHelp", listing_payload.get("isWillingToHelp"))
            ),
            "status": _clean(source.get("status")),
        }
        row["full_name"] = _full_name(row)
        if row["external_id"]:
            row["profile_url"] = f"{self.base_url}/user/{row['external_id']}"

        class_year = _category_field(profile, "Graduation year")
        if not class_year:
            class_year = _listing_section_value(listing_payload, "majorFieldsSection", 0, 0)
        row["class_year"] = class_year

        company_name = _company_name(profile)
        if not company_name:
            company_name = _listing_section_value(
                listing_payload, "professionalFieldsSection", 0, 0
            )
        row["company_name"] = company_name

        current_job = _category_field(profile, "Job title")
        if not current_job:
            current_job = _listing_section_value(
                listing_payload, "professionalFieldsSection", 0, 1
            )
        row["current_job"] = current_job

        loc = profile.get("location") if isinstance(profile.get("location"), dict) else {}
        row.update(
            {
                "address": _clean(loc.get("formattedAddress")),
                "city": _clean(loc.get("city")),
                "state": _clean(loc.get("state")),
                "country": _clean(loc.get("country")),
                "postal_code": _clean(loc.get("zipCode")),
                "lat": _clean(loc.get("lat")),
                "lng": _clean(loc.get("lng")),
            }
        )

        social_links = profile.get("userProfileSocialLinks") or []
        if isinstance(social_links, list):
            urls = [
                str(link.get("urlLink", "")).strip()
                for link in social_links
                if isinstance(link, dict) and link.get("urlLink")
            ]
            row["linkedin_profile_url"] = next(
                (url for url in urls if "linkedin.com" in url.lower()),
                "",
            )
            row["social_links"] = "; ".join(urls)

        work_experiences = profile.get("userWorkExperiences") or []
        if isinstance(work_experiences, list) and work_experiences:
            row["experiences"] = "; ".join(
                summary
                for summary in (
                    _summarize_experience(item)
                    for item in work_experiences
                    if isinstance(item, dict)
                )
                if summary
            )

        badges = source.get("badges") or listing_payload.get("badges")
        if isinstance(badges, list):
            row["badges"] = "; ".join(
                badge.get("name", "")
                for badge in badges
                if isinstance(badge, dict) and badge.get("name")
            )

        category_fields = profile.get("categoryFieldsForUserProfile") or []
        if isinstance(category_fields, list) and category_fields:
            for field in category_fields:
                if not isinstance(field, dict):
                    continue
                title = field.get("title")
                value = _category_field_value(field)
                if title and value:
                    row[_column_name(title)] = value
            row["category_fields_json"] = json.dumps(category_fields, ensure_ascii=True)

        return {key: value for key, value in row.items() if value not in (None, "")}


def _data(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    content = payload.get("content")
    if isinstance(content, dict):
        data = content.get("data")
        if isinstance(data, dict):
            return data
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    return str(value).strip()


def _full_name(row: dict) -> str:
    parts = [row.get("firstname", ""), row.get("lastname", "")]
    return " ".join(part for part in parts if part).strip()


def _category_field(profile: dict, title: str) -> str:
    fields = profile.get("categoryFieldsForUserProfile") or []
    if not isinstance(fields, list):
        return ""
    for field in fields:
        if isinstance(field, dict) and field.get("title") == title:
            return _category_field_value(field)
    return ""


def _category_field_value(field: dict) -> str:
    values = field.get("valuesForCategoryField") or []
    if not isinstance(values, list):
        return ""
    extracted = [
        str(item.get("value", "")).strip()
        for item in values
        if isinstance(item, dict) and item.get("value") is not None
    ]
    return "; ".join(value for value in extracted if value)


def _company_name(profile: dict) -> str:
    company = profile.get("company")
    if isinstance(company, dict):
        return _clean(company.get("name"))
    return ""


def _listing_section_value(
    listing_payload: dict,
    section_name: str,
    group_index: int,
    value_index: int,
) -> str:
    translations = listing_payload.get("categoryFieldItemTranslations")
    if not isinstance(translations, dict):
        return ""
    section = translations.get(section_name)
    try:
        value = section[group_index][value_index]
    except (TypeError, IndexError):
        return ""
    return _clean(value)


def _summarize_experience(item: dict) -> str:
    parts = []
    title = item.get("title") or item.get("position")
    company = item.get("companyName") or item.get("company")
    if isinstance(company, dict):
        company = company.get("name")
    if title:
        parts.append(str(title))
    if company:
        parts.append(f"@ {company}")
    return " ".join(parts)


def _column_name(name: str) -> str:
    return (
        name.lower()
        .replace("/", "_")
        .replace("-", "_")
        .replace(" ", "_")
        .strip("_")
    )
