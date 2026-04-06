"""
CSV exporter.

Flattens nested JSON profile data into a clean CSV with one row per alumni.
Dynamically discovers all fields — nothing is hardcoded.
"""

import csv
import logging

logger = logging.getLogger(__name__)


def export_to_csv(users: list[dict], output_path: str, full_profiles: bool = False) -> None:
    """Export user data to a UTF-8 CSV file."""
    if not users:
        logger.warning("No users to export.")
        return

    flat_users = []
    all_keys = set()

    for user in users:
        flat = _flatten_user(user, full_profiles)
        flat_users.append(flat)
        all_keys.update(flat.keys())

    # Priority columns first, then alphabetical
    priority_cols = [
        "id", "full_name", "firstname", "lastname", "prefix_name",
        "suffix_name", "maidenname", "class_year", "degree_type", "deceased",
        "headline", "email", "email2", "email3",
        "mobile_perso", "mobile_pro", "landline_perso", "landline_pro",
        "current_job", "company_name",
        "city", "state", "country", "postal_code", "address",
        "linkedin_profile_url", "instagram_profile_url",
        "facebook_profile_url", "twitter", "website",
        "preferred_paa", "affinity_groups", "sub_networks",
        "educations", "experiences", "skills", "industries",
        "awards", "birthday", "photo_url",
    ]

    ordered_cols = [c for c in priority_cols if c in all_keys]
    remaining = sorted(all_keys - set(ordered_cols))
    fieldnames = ordered_cols + remaining

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=fieldnames, restval="", extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(flat_users)

    logger.info(f"Exported {len(flat_users)} rows with {len(fieldnames)} columns")


def _flatten_user(user: dict, full_profiles: bool) -> dict:
    """Flatten a single user dict into a flat key-value structure."""
    flat = {}

    # ---- Basic scalar fields (from listing) ----
    for key in [
        "id", "firstname", "lastname", "prefix_name", "suffix_name",
        "maidenname", "headline", "deceased", "confirmed",
        "last_seen_at", "honorary_title",
    ]:
        if key in user:
            flat[key] = _clean(user.get(key))

    # ---- Custom fields array (from listing) ----
    for field in user.get("fields", []):
        display_name = field.get("display_name", "")
        value = field.get("value")

        if display_name == "Full Name":
            flat["full_name"] = _clean(value)
            flat["class_year"] = _extract_class_year(value or "")
        elif display_name == "Preferred PAA":
            flat["preferred_paa"] = _join(value)
        elif display_name == "Affinity Groups":
            flat["affinity_groups"] = _join(value)
        else:
            flat[_col_name(display_name)] = _join(value) if isinstance(value, list) else _clean(value)

    # ---- Location (from listing) ----
    loc = user.get("last_location")
    if isinstance(loc, dict) and loc:
        flat["city"] = _clean(loc.get("city"))
        flat["state"] = _clean(loc.get("administrative_area_level_1"))
        flat["country"] = _clean(loc.get("country"))
        flat["country_code"] = _clean(loc.get("country_code"))
        flat["address"] = _clean(loc.get("address"))
        flat["lat"] = _clean(loc.get("lat"))
        flat["lng"] = _clean(loc.get("lng"))

    # ---- Photo URL (skip placeholders) ----
    photo = user.get("photo")
    if isinstance(photo, dict):
        url = photo.get("original_url", "")
        flat["photo_url"] = "" if "missing/user_avatar" in url else url

    # ---- Full profile data ----
    profile = user.get("full_profile", {})
    if profile and full_profiles:
        _flatten_full_profile(profile, flat)

    return flat


def _flatten_full_profile(p: dict, flat: dict) -> None:
    """Merge all full-profile fields into the flat dict."""

    # --- Scalar contact / social fields ---
    for key in [
        "email", "email2", "email3",
        "mobile_pro", "mobile_perso",
        "landline_pro", "landline_perso",
        "current_job", "company_name",
        "linkedin_profile_url", "instagram_profile_url",
        "facebook_profile_url", "twitter", "website", "skype", "bbm",
        "birthday", "birthplace",
        "awards", "industry_name",
        "name", "headline",
    ]:
        val = p.get(key)
        if val is not None and val != "":
            flat[key] = _clean(val)

    # --- Full name from profile (overrides listing if present) ---
    if p.get("name"):
        flat["full_name"] = _clean(p["name"])

    # --- Location (richer than listing version) ---
    locations = p.get("locations", [])
    if locations:
        loc = locations[0]
        flat["city"] = _clean(loc.get("city"))
        flat["state"] = _clean(loc.get("administrative_area_level_1"))
        flat["country"] = _clean(loc.get("country"))
        flat["country_code"] = _clean(loc.get("country_code"))
        flat["postal_code"] = _clean(loc.get("postal_code"))
        flat["address"] = _clean(loc.get("address"))
        flat["neighborhood"] = _clean(loc.get("neighborhood"))
        flat["sublocality"] = _clean(loc.get("sublocality_level_1"))
        flat["lat"] = _clean(loc.get("lat"))
        flat["lng"] = _clean(loc.get("lng"))

    # --- Postal addresses ---
    postal = p.get("postal_address", {})
    for addr_type in ("work", "personal"):
        addr = postal.get(addr_type, {})
        if addr and any(addr.get(k) for k in ("address_1", "city", "postal_code", "country")):
            prefix = f"postal_{addr_type}"
            flat[f"{prefix}_address"] = _clean(addr.get("address_1"))
            flat[f"{prefix}_city"] = _clean(addr.get("city"))
            flat[f"{prefix}_state"] = _clean(addr.get("state"))
            flat[f"{prefix}_postal_code"] = _clean(addr.get("postal_code"))
            flat[f"{prefix}_country"] = _clean(addr.get("country"))

    # --- Education ---
    educations = p.get("educations", [])
    edu_rows = []
    for edu in educations:
        parts = []
        school = edu.get("school", {}).get("name", "")
        if school:
            parts.append(school)
        # Extract dynamic attributes (class year, degree, major, program type)
        for attr in edu.get("dynamic_attributes", []):
            val = attr.get("attr_value")
            if isinstance(val, list):
                parts.extend(str(v) for v in val if v)
            elif val:
                parts.append(str(val))
        if edu.get("to"):
            parts.append(f"to:{edu['to']}")
        if parts:
            edu_rows.append(" | ".join(parts))
    flat["educations"] = "; ".join(edu_rows)

    # Also extract first education's class year and degree for convenience
    if educations:
        for attr in educations[0].get("dynamic_attributes", []):
            val = attr.get("attr_value")
            # Class year is typically a 4-digit string
            if isinstance(val, str) and len(val) == 4 and val.isdigit():
                flat["class_year"] = val
            # Degree type
            if isinstance(val, list) and any("Bachelor" in str(v) or "Master" in str(v) or "PhD" in str(v) or "Certificate" in str(v) for v in val):
                flat["degree_type"] = "; ".join(str(v) for v in val)

    # --- Work experience ---
    experiences = p.get("experiences", [])
    exp_rows = []
    for exp in experiences:
        position = exp.get("position", "")
        company = exp.get("company", {}).get("name", "")
        from_date = exp.get("from", "")
        to_date = exp.get("to", "present")
        # Extract industry and role type from dynamic_attributes
        extra = []
        for attr in exp.get("dynamic_attributes", []):
            val = attr.get("attr_value")
            if isinstance(val, list):
                extra.extend(str(v) for v in val if v)
            elif val:
                extra.append(str(val))
        summary = f"{position} @ {company}"
        if from_date:
            summary += f" ({from_date} — {to_date or 'present'})"
        if extra:
            summary += f" [{', '.join(extra)}]"
        exp_rows.append(summary)
    flat["experiences"] = "; ".join(exp_rows)

    # --- Sub-networks (class, regional groups) ---
    sub_networks = p.get("sub_networks", [])
    flat["sub_networks"] = "; ".join(sn.get("title", "") for sn in sub_networks if sn.get("title"))

    # --- Skills ---
    skills = p.get("skills", [])
    if skills:
        flat["skills"] = "; ".join(
            s.get("name", "") if isinstance(s, dict) else str(s) for s in skills
        )

    # --- Industries ---
    industries = p.get("industries", [])
    if industries:
        flat["industries"] = "; ".join(
            ind.get("name", "") if isinstance(ind, dict) else str(ind) for ind in industries
        )

    # --- Privacy / sharing settings (might be useful context) ---
    for key in [
        "share_email", "share_email2", "share_email3",
        "share_mobile_pro", "share_mobile_perso",
    ]:
        val = p.get(key)
        if val:
            flat[key] = _clean(val)

    # --- Profile metadata ---
    flat["profile_is_private"] = _clean(p.get("profile_is_private"))
    flat["is_active"] = _clean(p.get("is_active"))
    flat["current_sign_in_at"] = _clean(p.get("current_sign_in_at"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_class_year(full_name: str) -> str:
    """Extract class year from full name like "Ms. Charlotte Y. Stanton '00"."""
    import re
    match = re.search(r"['\*](\d{2})\b", full_name)
    if match:
        year = int(match.group(1))
        return str(2000 + year) if year <= 30 else str(1900 + year)
    return ""


def _clean(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, (int, float)):
        return str(val)
    return str(val).strip()


def _join(val) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        return "; ".join(str(v) for v in val if v)
    return str(val)


def _col_name(name: str) -> str:
    return name.lower().replace(" ", "_").replace("/", "_").replace("-", "_")