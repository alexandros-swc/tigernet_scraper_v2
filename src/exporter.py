"""
CSV exporter.

Flattens nested JSON profile data into a clean CSV with one row per alumni.
Dynamically discovers all fields — nothing is hardcoded.
"""

import csv
import logging

logger = logging.getLogger(__name__)


def export_to_csv(users: list[dict], output_path: str, full_profiles: bool = False) -> None:
    """
    Export user data to a UTF-8 CSV file.

    Handles:
    - Flattening nested objects (location, fields, etc.)
    - Dynamic field discovery (no hardcoded column list)
    - Missing values as empty strings
    - Special character escaping
    """
    if not users:
        logger.warning("No users to export.")
        return

    # Flatten all users and collect all unique field names
    flat_users = []
    all_keys = set()

    for user in users:
        flat = _flatten_user(user, full_profiles)
        flat_users.append(flat)
        all_keys.update(flat.keys())

    # Sort columns: put important ones first, then alphabetical
    priority_cols = [
        "id", "full_name", "firstname", "lastname", "prefix_name",
        "suffix_name", "maidenname", "class_year", "deceased",
        "headline", "email", "email2", "email3",
        "mobile_perso", "mobile_pro", "landline_perso", "landline_pro",
        "current_job", "company_name",
        "city", "state", "country", "address",
        "linkedin_profile_url", "instagram_profile_url",
        "facebook_profile_url", "twitter", "website",
        "preferred_paa", "affinity_groups",
        "photo_url",
    ]

    ordered_cols = [c for c in priority_cols if c in all_keys]
    remaining = sorted(all_keys - set(ordered_cols))
    fieldnames = ordered_cols + remaining

    # Write CSV
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            restval="",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(flat_users)

    logger.info(f"Exported {len(flat_users)} rows with {len(fieldnames)} columns")


def _flatten_user(user: dict, full_profiles: bool) -> dict:
    """Flatten a single user dict into a flat key-value structure."""
    flat = {}

    # Basic scalar fields
    for key in [
        "id", "firstname", "lastname", "prefix_name", "suffix_name",
        "maidenname", "headline", "deceased", "confirmed",
        "last_seen_at", "honorary_title",
    ]:
        if key in user:
            flat[key] = _clean_value(user.get(key))

    # Extract Full Name and class year from the fields array
    fields = user.get("fields", [])
    for field in fields:
        display_name = field.get("display_name", "")
        value = field.get("value")

        if display_name == "Full Name":
            flat["full_name"] = _clean_value(value)
            # Try to extract class year from the full name (e.g., "'84", "*09")
            flat["class_year"] = _extract_class_year(value or "")

        elif display_name == "Preferred PAA":
            flat["preferred_paa"] = _join_list(value)

        elif display_name == "Affinity Groups":
            flat["affinity_groups"] = _join_list(value)

        else:
            # Capture any other custom fields dynamically
            col_name = _sanitize_column_name(display_name)
            flat[col_name] = _join_list(value) if isinstance(value, list) else _clean_value(value)

    # Location data
    location = user.get("last_location")
    if isinstance(location, dict) and location:
        flat["city"] = _clean_value(location.get("city"))
        flat["state"] = _clean_value(location.get("administrative_area_level_1"))
        flat["country"] = _clean_value(location.get("country"))
        flat["country_code"] = _clean_value(location.get("country_code"))
        flat["address"] = _clean_value(location.get("address"))
        flat["lat"] = _clean_value(location.get("lat"))
        flat["lng"] = _clean_value(location.get("lng"))

    # Photo URL (just the main one, skip all the variants)
    photo = user.get("photo")
    if isinstance(photo, dict):
        photo_url = photo.get("original_url", "")
        # Skip placeholder images
        if photo_url and "missing/user_avatar" not in photo_url:
            flat["photo_url"] = photo_url
        else:
            flat["photo_url"] = ""
    flat["photo_thumb_url"] = _clean_value(user.get("photo_thumb_url", ""))

    # Full profile data (if fetched)
    profile = user.get("full_profile", {})
    if profile and full_profiles:
        _flatten_full_profile(profile, flat)

    return flat


def _flatten_full_profile(profile: dict, flat: dict) -> None:
    """Add full profile fields to the flat dict."""

    # Contact info
    for key in [
        "email", "email2", "email3",
        "mobile_pro", "mobile_perso",
        "landline_pro", "landline_perso",
        "current_job", "company_name",
        "linkedin_profile_url", "instagram_profile_url",
        "facebook_profile_url", "twitter", "website", "skype",
        "birthday", "birthplace",
    ]:
        if key in profile:
            flat[key] = _clean_value(profile.get(key))

    # Education — flatten into a semicolon-separated summary
    educations = profile.get("educations", [])
    edu_summaries = []
    for edu in educations:
        parts = []
        school = edu.get("school", {}).get("name", "")
        if school:
            parts.append(school)

        # Extract degree, major, class year from dynamic_attributes
        for attr in edu.get("dynamic_attributes", []):
            val = attr.get("attr_value")
            if isinstance(val, list):
                parts.extend(val)
            elif val:
                parts.append(str(val))

        if parts:
            edu_summaries.append(" — ".join(parts))

    flat["educations"] = "; ".join(edu_summaries)

    # Work experience — flatten into a semicolon-separated summary
    experiences = profile.get("experiences", [])
    exp_summaries = []
    for exp in experiences:
        position = exp.get("position", "")
        company = exp.get("company", {}).get("name", "")
        from_date = exp.get("from", "")
        to_date = exp.get("to", "present")
        if position or company:
            summary = f"{position} at {company}"
            if from_date:
                summary += f" ({from_date} — {to_date or 'present'})"
            exp_summaries.append(summary)

    flat["experiences"] = "; ".join(exp_summaries)

    # Sub-networks (classes, regional groups)
    sub_networks = profile.get("sub_networks", [])
    flat["sub_networks"] = "; ".join(
        sn.get("title", "") for sn in sub_networks if sn.get("title")
    )

    # Skills
    skills = profile.get("skills", [])
    if skills:
        flat["skills"] = "; ".join(
            s.get("name", "") if isinstance(s, dict) else str(s)
            for s in skills
        )


def _extract_class_year(full_name: str) -> str:
    """
    Extract class year from full name string.
    
    Examples:
        "Ms. Charlotte Y. Stanton '00 " → "2000"
        "Mr. Stephen P. Ban '84 S88 P23 " → "1984"
        "Mr. Ledio Cakaj *09 S98 " → "2009"
    """
    import re
    # Look for 'YY or *YY pattern (undergrad or grad)
    match = re.search(r"['\*](\d{2})\b", full_name)
    if match:
        year = int(match.group(1))
        # Assume 00-30 = 2000s, 31-99 = 1900s
        if year <= 30:
            return str(2000 + year)
        else:
            return str(1900 + year)
    return ""


def _clean_value(val) -> str:
    """Convert a value to a clean string for CSV output."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, (int, float)):
        return str(val)
    return str(val).strip()


def _join_list(val) -> str:
    """Join a list into a semicolon-separated string."""
    if val is None:
        return ""
    if isinstance(val, list):
        return "; ".join(str(v) for v in val if v)
    return str(val)


def _sanitize_column_name(name: str) -> str:
    """Convert a display name to a safe column name."""
    return name.lower().replace(" ", "_").replace("/", "_").replace("-", "_")
