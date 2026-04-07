"""
CSV exporter.

Flattens nested JSON profile data into a clean CSV with one row per alumni.

IMPORTANT: No hardcoded field lists. Every field in the API response is captured
dynamically. Known fields get friendly column names, but unknown/new fields
are included automatically with auto-generated names.
"""

import csv
import logging
import re

logger = logging.getLogger(__name__)

# Fields to skip — internal metadata, redundant photo variants, permissions, etc.
SKIP_FIELDS = {
    "new_photo", "new_cover_picture",  # Redundant photo URL variants
    "cover_picture_url", "cover_picture_medium_url", "cover_picture_is_default",
    "photo_is_default", "photo_medium_url",
    "guid", "locale", "timezone",  # Internal metadata
    "signup_payment_required", "landing_page_path", "welcome_page_path",
    "total_unread_messages", "users_can_create_events", "user_can_access_events",
    "can_create_forum_post", "can_access_forum", "can_invite_users",
    "profile_is_editable", "can", "journeys_rights", "user_targeting",
    "primary_email_choice", "default_billing_address_type",
    "has_introduction", "can_message_user", "can_access_to_contact", "cant_access_message",
}

# Priority columns — these appear first in the CSV (if present)
PRIORITY_COLUMNS = [
    "id", "full_name", "firstname", "lastname", "prefix_name",
    "suffix_name", "maidenname", "class_year", "degree_type", "deceased",
    "headline", "email", "email2", "email3",
    "mobile_perso", "mobile_pro", "landline_perso", "landline_pro",
    "current_job", "company_name",
    "city", "state", "country", "postal_code", "address",
    "linkedin_profile_url", "instagram_profile_url",
    "facebook_profile_url", "twitter", "website", "skype",
    "student_activities", "primary_affiliation", "primary_class_degree_year",
    "affiliation_s_", "class_degree_year_s__of_affiliations", "regions",
    "preferred_paa", "affinity_groups", "sub_networks",
    "nickname", "volunteer_activity_1_",
    "educations", "experiences", "skills", "industries",
    "awards", "birthday", "birthplace", "photo_url",
]


def export_to_csv(users: list[dict], output_path: str, full_profiles: bool = False) -> None:
    """Export user data to a UTF-8 CSV file."""
    if not users:
        logger.warning("No users to export.")
        return

    flat_users = []
    all_keys = set()

    for i, user in enumerate(users):
        flat = _flatten_user(user, full_profiles)
        flat_users.append(flat)
        all_keys.update(flat.keys())
        if i == 0:
            logger.info(f"DEBUG first user flat keys ({len(flat)} total): {sorted(flat.keys())}")

    # Order columns: priority first, then alphabetical
    ordered = [c for c in PRIORITY_COLUMNS if c in all_keys]
    remaining = sorted(all_keys - set(ordered))
    fieldnames = ordered + remaining

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(flat_users)

    logger.info(f"Exported {len(flat_users)} rows with {len(fieldnames)} columns")


def _flatten_user(user: dict, full_profiles: bool) -> dict:
    """Flatten a single user dict into a flat key-value structure."""
    flat = {}

    # ---- Dynamically capture all top-level scalar fields from listing ----
    for key, val in user.items():
        if key in SKIP_FIELDS:
            continue
        if key in ("fields", "photo", "last_location", "full_profile"):
            continue  # Handled specially below
        if isinstance(val, (str, int, float, bool)) or val is None:
            flat[key] = _clean(val)

    # ---- Custom fields array (from listing) — fully dynamic ----
    for field in user.get("fields", []):
        display_name = field.get("display_name", "")
        value = field.get("value")
        col = _col_name(display_name)

        if display_name == "Full Name":
            flat["full_name"] = _clean(value)
            flat["class_year"] = _extract_class_year(value or "")
        else:
            flat[col] = _join(value) if isinstance(value, list) else _clean(value)

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

    # ---- Profile data endpoint (student activities, volunteer work, etc.) ----
    profile_data = user.get("profile_data", {})
    if profile_data and full_profiles:
        # Debug: log what we got
        import logging
        _logger = logging.getLogger(__name__)
        if isinstance(profile_data, dict):
            _logger.debug(f"profile_data keys: {list(profile_data.keys())}")
            center = profile_data.get("center", [])
            if isinstance(center, list):
                for sec in center:
                    if isinstance(sec, dict):
                        sec_name = sec.get("name", "?")
                        sec_data = sec.get("data", [])
                        field_names = []
                        if isinstance(sec_data, list):
                            for item in sec_data:
                                if isinstance(item, dict):
                                    dn = item.get("display_name", "")
                                    val = item.get("value")
                                    if dn:
                                        field_names.append(f"{dn}={'...' if val else 'null'}")
                        _logger.debug(f"  Section '{sec_name}': {field_names}")
        _flatten_profile_data(profile_data, flat)

    return flat


def _flatten_full_profile(p: dict, flat: dict) -> None:
    """
    Dynamically flatten all full-profile fields.
    
    Scalar values are captured directly. Known nested structures 
    (educations, experiences, etc.) get special formatting.
    Anything unknown is still captured.
    """

    # --- Pass 1: Capture ALL scalar fields dynamically ---
    for key, val in p.items():
        if key in SKIP_FIELDS:
            continue
        # Skip nested structures — handled in Pass 2
        if isinstance(val, (dict, list)):
            continue
        if val is not None and val != "":
            flat[key] = _clean(val)

    # Override full_name from profile if present
    if p.get("name"):
        flat["full_name"] = _clean(p["name"])

    # --- Pass 2: Handle known nested structures with nice formatting ---

    # Location (richer than listing)
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

    # Postal addresses
    postal = p.get("postal_address", {})
    if isinstance(postal, dict):
        for addr_type in ("work", "personal"):
            addr = postal.get(addr_type, {})
            if isinstance(addr, dict) and any(addr.get(k) for k in addr):
                prefix = f"postal_{addr_type}"
                for ak, av in addr.items():
                    if av is not None and av != "":
                        flat[f"{prefix}_{ak}"] = _clean(av)

    # Education — dynamic attribute extraction
    educations = p.get("educations", [])
    edu_rows = []
    for edu in educations:
        parts = []
        school = edu.get("school", {}).get("name", "")
        if school:
            parts.append(school)
        if edu.get("degree"):
            parts.append(str(edu["degree"]))
        if edu.get("field_of_study"):
            parts.append(str(edu["field_of_study"]))
        for attr in edu.get("dynamic_attributes", []):
            val = attr.get("attr_value")
            if isinstance(val, list):
                parts.extend(_stringify(v) for v in val if v)
            elif val is not None:
                parts.append(_stringify(val))
        if edu.get("from"):
            parts.append(f"from:{edu['from']}")
        if edu.get("to"):
            parts.append(f"to:{edu['to']}")
        if parts:
            edu_rows.append(" | ".join(parts))
    if edu_rows:
        flat["educations"] = "; ".join(edu_rows)

    # Extract class year and degree from first education for convenience
    if educations:
        for attr in educations[0].get("dynamic_attributes", []):
            val = attr.get("attr_value")
            if isinstance(val, str) and len(val) == 4 and val.isdigit():
                flat["class_year"] = val
            if isinstance(val, list):
                for v in val:
                    sv = str(v)
                    if any(d in sv for d in ("Bachelor", "Master", "PhD", "Certificate", "Doctor")):
                        flat["degree_type"] = sv

    # Work experience — dynamic attribute extraction
    experiences = p.get("experiences", [])
    exp_rows = []
    for exp in experiences:
        parts_main = []
        position = exp.get("position", "")
        company = exp.get("company", {}).get("name", "")
        if position:
            parts_main.append(position)
        if company:
            parts_main.append(f"@ {company}")
        from_date = exp.get("from", "")
        to_date = exp.get("to", "")
        if from_date or to_date:
            parts_main.append(f"({from_date or '?'} — {to_date or 'present'})")
        # Capture dynamic attributes
        extras = []
        for attr in exp.get("dynamic_attributes", []):
            val = attr.get("attr_value")
            if isinstance(val, list):
                extras.extend(_stringify(v) for v in val if v)
            elif val is not None:
                extras.append(_stringify(val))
        summary = " ".join(parts_main)
        if extras:
            summary += f" [{', '.join(extras)}]"
        if summary.strip():
            exp_rows.append(summary)
    if exp_rows:
        flat["experiences"] = "; ".join(exp_rows)

    # Sub-networks
    sub_networks = p.get("sub_networks", [])
    if sub_networks:
        flat["sub_networks"] = "; ".join(
            sn.get("title", "") for sn in sub_networks if sn.get("title")
        )

    # Skills
    skills = p.get("skills", [])
    if skills:
        flat["skills"] = "; ".join(
            s.get("name", "") if isinstance(s, dict) else str(s) for s in skills
        )

    # Industries
    industries = p.get("industries", [])
    if industries:
        flat["industries"] = "; ".join(
            ind.get("name", "") if isinstance(ind, dict) else str(ind) for ind in industries
        )

    # Experience industries
    exp_industries = p.get("experience_industries", [])
    if exp_industries:
        flat["experience_industries"] = "; ".join(
            ind.get("name", "") if isinstance(ind, dict) else str(ind) for ind in exp_industries
        )

    # --- Pass 3: Catch any remaining nested fields we didn't handle ---
    handled_nested = {
        "locations", "last_location", "postal_address",
        "educations", "experiences", "sub_networks",
        "skills", "industries", "experience_industries",
        "new_photo", "new_cover_picture",
        "can", "journeys_rights", "user_targeting",
    }
    for key, val in p.items():
        if key in SKIP_FIELDS or key in handled_nested:
            continue
        if isinstance(val, list) and val and key not in flat:
            # Unknown list field — serialize it
            flat[key] = "; ".join(_serialize_item(item) for item in val)
        elif isinstance(val, dict) and key not in flat:
            # Unknown dict field — serialize it
            flat[key] = _serialize_item(val)


def _flatten_profile_data(pd: dict, flat: dict) -> None:
    """
    Flatten the /data endpoint response which contains Student Activities,
    Volunteer Activities, Princeton Information, and other custom fields.
    
    The structure is: { "center": [...sections...], "contact": [...sections...] }
    Each section has a "data" array of field objects with "display_name" and "value".
    
    This is fully dynamic — any field with a non-null value gets captured.
    """
    # Process both "center" and "contact" section groups
    for section_group_key in ("center", "contact"):
        sections = pd.get(section_group_key, [])
        if not isinstance(sections, list):
            continue

        for section in sections:
            if not isinstance(section, dict):
                continue

            data_items = section.get("data", [])
            if not isinstance(data_items, list):
                continue

            for item in data_items:
                if not isinstance(item, dict):
                    continue

                # Some items are nested sections (e.g., contact > emails > data)
                if "data" in item and isinstance(item["data"], list):
                    # Recurse into nested section
                    for nested_item in item["data"]:
                        _extract_data_field(nested_item, flat)
                else:
                    _extract_data_field(item, flat)


def _extract_data_field(item: dict, flat: dict) -> None:
    """Extract a single field from the /data endpoint into the flat dict."""
    if not isinstance(item, dict):
        return

    display_name = item.get("display_name", "")
    value = item.get("value")

    # Skip null/empty values
    if value is None or value == "" or value == []:
        return

    # Skip metadata fields we already get from other endpoints
    skip_names = {
        "Primary email", "Alternate Email 1", "Alternate Email 2",
        "Preferred contact email", "Personal mobile", "Work landline",
        "Landline personal", "Custom contact access message",
        "Facebook profile URL", "Instagram profile URL",
        "X profile URL", "LinkedIn profile url", "Website",
    }
    if display_name in skip_names:
        return

    col = _col_name(display_name)

    if isinstance(value, list):
        flat[col] = "; ".join(_stringify(v) for v in value if v)
    else:
        flat[col] = _clean(value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_item(item) -> str:
    """Convert an unknown nested item to a readable string."""
    if isinstance(item, dict):
        parts = []
        for k, v in item.items():
            if v is not None and v != "" and not isinstance(v, (dict, list)):
                parts.append(f"{k}={v}")
        return ", ".join(parts) if parts else ""
    return str(item)


def _stringify(val) -> str:
    """Safely convert any value to a string — handles dicts, lists, scalars."""
    if val is None:
        return ""
    if isinstance(val, dict):
        return _serialize_item(val)
    if isinstance(val, list):
        return "; ".join(_stringify(v) for v in val if v)
    return str(val)


def _extract_class_year(full_name: str) -> str:
    """Extract class year from full name like "Ms. Charlotte Y. Stanton '00"."""
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